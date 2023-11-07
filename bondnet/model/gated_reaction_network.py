import torch
import itertools
import numpy as np
import dgl
from bondnet.model.gated_mol import GatedGCNMol


class GatedGCNReactionNetwork(GatedGCNMol):
    def forward(self, graph, feats, reactions, norm_atom=None, norm_bond=None):
        """
        Args:
            graph (DGLHeteroGraph or BatchedDGLHeteroGraph): (batched) molecule graphs
            feats (dict): node features with node type as key and the corresponding
                features as value.
            reactions (list): a sequence of :class:`bondnet.data.reaction_network.Reaction`,
                each representing a reaction.
            norm_atom (2D tensor or None): graph norm for atom
            norm_bond (2D tensor or None): graph norm for bond

        Returns:
            2D tensor: of shape(N, M), where `M = outdim`.
        """

        # embedding
        feats = self.embedding(feats)

        # gated layer
        for layer in self.gated_layers:
            feats = layer(graph, feats, norm_atom, norm_bond)

        # convert mol graphs to reaction graphs by subtracting reactant feats from
        # products feats
        graph, feats = mol_graph_to_rxn_graph(graph, feats, reactions)

        # readout layer
        feats = self.readout_layer(graph, feats)

        # fc
        for layer in self.fc_layers:
            feats = layer(feats)

        return feats

    def feature_before_fc(self, graph, feats, reactions, norm_atom, norm_bond):
        """
        Get the features before the final fully-connected.

        This is used for feature visualization.
        """
        # embedding
        feats = self.embedding(feats)

        # gated layer
        for layer in self.gated_layers:
            feats = layer(graph, feats, norm_atom, norm_bond)

        # convert mol graphs to reaction graphs by subtracting reactant feats from
        # products feats
        graph, feats = mol_graph_to_rxn_graph(graph, feats, reactions)

        # readout layer
        feats = self.readout_layer(graph, feats)

        return feats

    def feature_at_each_layer(self, graph, feats, reactions, norm_atom, norm_bond):
        """
        Get the features at each layer before the final fully-connected layer.

        This is used for feature visualization to see how the model learns.

        Returns:
            dict: (layer_idx, feats), each feats is a list of
        """

        layer_idx = 0
        all_feats = dict()

        # embedding
        feats = self.embedding(feats)

        # store bond feature of each molecule
        fts = _split_batched_output(graph, feats["bond"])
        all_feats[layer_idx] = fts
        layer_idx += 1

        # gated layer
        for layer in self.gated_layers:
            feats = layer(graph, feats, norm_atom, norm_bond)

            # store bond feature of each molecule
            fts = _split_batched_output(graph, feats["bond"])
            all_feats[layer_idx] = fts
            layer_idx += 1

        return all_feats


def _split_batched_output(graph, value):
    """
    Split a tensor into `num_graphs` chunks, the size of each chunk equals the
    number of bonds in the graph.

    Returns:
        list of tensor.

    """
    nbonds = graph.batch_num_nodes("bond")
    return torch.split(value, nbonds)


def mol_graph_to_rxn_graph(graph, feats, reactions):
    """
    Convert a batched molecule graph to a batched reaction graph.

    Essentially, a reaction graph has the same graph structure as the reactant and
    its features are the difference between the products features and reactant features.

    Args:
        graph (BatchedDGLHeteroGraph): batched graph representing molecules.
        feats (dict): node features with node type as key and the corresponding
            features as value.
        reactions (list): a sequence of :class:`bondnet.data.reaction_network.Reaction`,
            each representing a reaction.

    Returns:
        batched_graph (BatchedDGLHeteroGraph): a batched graph representing a set of
            reactions.
        feats (dict): features for the batched graph
    """
    # TODO add graph.local_var() since hetero and homo graphs are combined
    # should not use graph.local_var() to make a local copy, since it converts a
    # BatchedDGLHeteroGraph into a DGLHeteroGraph. Then unbatch_hetero(graph) below
    # will not work.
    # If you really want to, use copy.deepcopy() to make a local copy

    # assign feats

    graph = graph.to('cuda:0')
    for nt, ft in feats.items():
        # ft = ft.to('cuda:0')
        graph.nodes[nt].data.update({"ft": ft})

    # unbatch molecule graph
    graphs = dgl.unbatch(graph)

    # create reaction graphs
    reaction_graphs = []
    reaction_feats = []
    for rxn in reactions:
        reactants = [graphs[i] for i in rxn.reactants]
        products = [graphs[i] for i in rxn.products]

        # whether a molecule has bonds?
        has_bonds = {
            # we support only one reactant now, so no it is assumed always to have bond
            "reactants": [True for _ in reactants],
            "products": [True if len(mp) > 0 else False for mp in rxn.bond_mapping],
        }
        mappings = {"atom": rxn.atom_mapping_as_list, "bond": rxn.bond_mapping_as_list}

        g, fts = create_rxn_graph(
            reactants, products, mappings, has_bonds, tuple(feats.keys())
        )
        reaction_graphs.append(g)
        reaction_feats.append(fts)

    # batched reaction graph and data
    batched_graph = dgl.batch(reaction_graphs)
    batched_feats = {}
    for nt in feats:
        batched_feats[nt] = torch.cat([ft[nt] for ft in reaction_feats])

    return batched_graph, batched_feats


def create_rxn_graph(
    reactants,
    products,
    mappings,
    has_bonds,
    ntypes=("atom", "bond", "global"),
    ft_name="ft",
):
    """
    A reaction is represented by:

    feats of products - feats of reactant

    Args:
        reactants (list of DGLHeteroGraph): a sequence of reactants graphs
        products (list of DGLHeteroGraph): a sequence of product graphs
        mappings (dict): with node type as the key (e.g. `atom` and `bond`) and a list
            as value, which is a mapping between reactant feature and product feature
            of the same atom (bond).
        has_bonds (dict): whether the reactants and products have bonds.
        ntypes (list): node types of which the feature are manipulated
        ft_name (str): key of feature inf data dict

    Returns:
        graph (DGLHeteroGraph): a reaction graph with feats constructed from between
            reactant and products.
        feats (dict): features of reaction graph
    """
    assert len(reactants) == 1, f"number of reactants ({len(reactants)}) not supported"

    # note, this assumes we have one reactant
    graph = reactants[0]

    feats = dict()
    for nt in ntypes:
        reactants_ft = [p.nodes[nt].data[ft_name] for p in reactants]
        products_ft = [p.nodes[nt].data[ft_name] for p in products]

        # remove bond ft if the corresponding molecule has no bond
        # this is necessary because, to make heterogeneous graph work, we create
        # factitious bond features for molecule without any bond (i.e. single atom
        # molecule, e.g. H+)
        if nt == "bond":
            # select the ones that has bond
            # note, this assumes only one bond missing in products
            ## reactants_ft = list(itertools.compress(reactants_ft, has_bonds["reactants"]))
            products_ft = list(itertools.compress(products_ft, has_bonds["products"]))

            # add a feature with all zeros for the broken bond
            products_ft.append(reactants_ft[0].new_zeros((1, reactants_ft[0].shape[1])))

        reactants_ft = torch.cat(reactants_ft)
        products_ft = torch.cat(products_ft)

        if nt == "global":
            reactants_ft = torch.sum(reactants_ft, dim=0, keepdim=True)
            products_ft = torch.sum(products_ft, dim=0, keepdim=True)
        else:
            # reorder products_ft such that atoms (bonds) have the same order as reactants
            assert len(products_ft) == len(mappings[nt]), (
                f"products_ft ({len(products_ft)}) and mappings[{nt}] "
                f"({len(mappings[nt])}) have different length"
            )
            products_ft = products_ft[mappings[nt]]

        feats[nt] = products_ft - reactants_ft

    return graph, feats
