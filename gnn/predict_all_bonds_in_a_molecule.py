import os
import sys
import torch
import yaml
import numpy as np
import gnn
from gnn.model.gated_reaction_network import GatedGCNReactionNetwork
from gnn.data.electrolyte import ElectrolyteReactionNetworkDataset
from gnn.data.dataloader import DataLoaderReactionNetwork
from gnn.data.grapher import HeteroMoleculeGraph
from gnn.data.featurizer import (
    AtomFeaturizer,
    BondAsNodeFeaturizer,
    GlobalFeaturizerCharge,
)
from gnn.core.prediction import PredictionByOneReactant
from gnn.utils import load_checkpoints
from rdkit import RDLogger

# RDLogger.logger().setLevel(RDLogger.CRITICAL)


def get_predictor(molecule, format, charge, model="20200422"):

    # training using the electrolyte dataset
    if model == "20200422":
        allowed_charge = [-1, 0, 1]
    # training using the nrel bde dataset
    else:
        charge = 0
        allowed_charge = [0]

    predictor = PredictionByOneReactant(
        molecule, format, charge, allowed_charge, ring_bond=False
    )
    sdf_file = "/tmp/struct.sdf"
    label_file = "/tmp/label.yaml"
    feature_file = "/tmp/feature.yaml"
    predictor.prepare_data(sdf_file, label_file, feature_file)

    return predictor, sdf_file, label_file, feature_file


def evaluate(model, nodes, data_loader, device=None):
    model.eval()

    predictions = []
    with torch.no_grad():

        for it, (bg, label) in enumerate(data_loader):
            feats = {nt: bg.nodes[nt].data["feat"] for nt in nodes}
            norm_atom = label["norm_atom"]
            norm_bond = label["norm_bond"]
            mean = label["scaler_mean"]
            stdev = label["scaler_stdev"]

            if device is not None:
                feats = {k: v.to(device) for k, v in feats.items()}
                norm_atom = norm_atom.to(device)
                norm_bond = norm_bond.to(device)

            pred = model(bg, feats, label["reaction"], norm_atom, norm_bond)
            pred = pred.view(-1)
            pred = (pred * stdev + mean).cpu().numpy()

            predictions.append(pred)

    predictions = np.concatenate(predictions)

    return predictions


def get_grapher():
    atom_featurizer = AtomFeaturizer()
    bond_featurizer = BondAsNodeFeaturizer(length_featurizer=None)
    global_featurizer = GlobalFeaturizerCharge()
    grapher = HeteroMoleculeGraph(
        atom_featurizer=atom_featurizer,
        bond_featurizer=bond_featurizer,
        global_featurizer=global_featurizer,
        self_loop=True,
    )
    return grapher


def main(molecule, format, charge, model="20200422"):

    # convert input to model files
    predictor, sdf_file, label_file, feature_file = get_predictor(
        molecule, format, charge, model
    )

    # TODO add check for species here (whether species supported by dataset)

    # load dataset
    model_dir = os.path.join(os.path.dirname(gnn.__file__), "pre_trained", model)
    dataset = ElectrolyteReactionNetworkDataset(
        grapher=get_grapher(),
        sdf_file=sdf_file,
        label_file=label_file,
        feature_file=feature_file,
        feature_transformer=True,
        label_transformer=True,
        state_dict_filename=os.path.join(model_dir, "dataset_state_dict.pkl"),
    )
    data_loader = DataLoaderReactionNetwork(dataset, batch_size=100, shuffle=False)

    # model
    feature_names = ["atom", "bond", "global"]
    # feature_names = ["atom", "bond"]

    # NOTE cannot use gnn.utils.yaml_load, seems a bug in yaml.
    #  see: https://github.com/yaml/pyyaml/issues/266
    with open(os.path.join(model_dir, "train_args.yaml"), "r") as f:
        model_args = yaml.load(f, Loader=yaml.Loader)

    model = GatedGCNReactionNetwork(
        in_feats=model_args.feature_size,
        embedding_size=model_args.embedding_size,
        gated_num_layers=model_args.gated_num_layers,
        gated_hidden_size=model_args.gated_hidden_size,
        gated_num_fc_layers=model_args.gated_num_fc_layers,
        gated_graph_norm=model_args.gated_graph_norm,
        gated_batch_norm=model_args.gated_batch_norm,
        gated_activation=model_args.gated_activation,
        gated_residual=model_args.gated_residual,
        gated_dropout=model_args.gated_dropout,
        num_lstm_iters=model_args.num_lstm_iters,
        num_lstm_layers=model_args.num_lstm_layers,
        set2set_ntypes_direct=model_args.set2set_ntypes_direct,
        fc_num_layers=model_args.fc_num_layers,
        fc_hidden_size=model_args.fc_hidden_size,
        fc_batch_norm=model_args.fc_batch_norm,
        fc_activation=model_args.fc_activation,
        fc_dropout=model_args.fc_dropout,
        outdim=1,
        conv="GatedGCNConv",
    )
    load_checkpoints({"model": model}, filename=os.path.join(model_dir, "checkpoint.pkl"))

    # evaluate
    predictions = evaluate(model, feature_names, data_loader)

    # in case some entry fail
    if len(predictions) != len(dataset.failed):
        pred = []
        idx = 0
        for failed in dataset.failed:
            if failed:
                pred.append(None)
            else:
                pred.append(predictions[idx])
                idx += 1
        predictions = pred

    # write the results
    predictor.write_results(predictions, to_stdout=True)


if __name__ == "__main__":
    molecule = "C1=CC=CC=C1"
    main(molecule, format="smiles", charge=0, model="20200422")

    molecule = "InChI=1S/C6H6/c1-2-4-6-5-3-1/h1-6H"
    main(molecule, format="inchi", charge=0, model="20200422")

    molecule = """
     RDKit          3D

  0  0  0  0  0  0  0  0  0  0999 V3000
M  V30 BEGIN CTAB
M  V30 COUNTS 12 12 0 0 0
M  V30 BEGIN ATOM
M  V30 1 C 1.36446 0.289447 0.00138143 0
M  V30 2 C 0.431589 1.32633 -0.0115312 0
M  V30 3 C -0.932873 1.03688 -0.0129012 0
M  V30 4 C -1.36446 -0.289446 -0.00136503 0
M  V30 5 C -0.431588 -1.32632 0.0115338 0
M  V30 6 C 0.932875 -1.03688 0.0129073 0
M  V30 7 H 2.42754 0.514962 0.00245851 0
M  V30 8 H 0.767844 2.35969 -0.0205258 0
M  V30 9 H -1.65969 1.84473 -0.0229591 0
M  V30 10 H -2.42753 -0.514962 -0.0024474 0
M  V30 11 H -0.767849 -2.35968 0.0204957 0
M  V30 12 H 1.65969 -1.84473 0.022953 0
M  V30 END ATOM
M  V30 BEGIN BOND
M  V30 1 2 1 2
M  V30 2 1 2 3
M  V30 3 2 3 4
M  V30 4 1 4 5
M  V30 5 2 5 6
M  V30 6 1 6 1
M  V30 7 1 1 7
M  V30 8 1 2 8
M  V30 9 1 3 9
M  V30 10 1 4 10
M  V30 11 1 5 11
M  V30 12 1 6 12
M  V30 END BOND
M  V30 END CTAB
M  END
"""
    main(molecule, format="sdf", charge=0, model="20200422")
