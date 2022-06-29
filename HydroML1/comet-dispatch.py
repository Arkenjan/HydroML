# Script for hyperparameter training on cluster
import argparse

from Hyd_ML import train_test_everything
import sys

from Util import *

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train with a set of hyperparameters')
    parser.add_argument('--flow_between_stores', type=bool, nargs='?', default=False,
                        help='Whether to model flow_between_stores')
    parser.add_argument('--num_stores', type=int, default=8, nargs='?',
                        help='')
    parser.add_argument('--reload', type=int, default=0, nargs='?',
                        help='Whether to reload the last model from the same directory (E200)')

    args = parser.parse_args()

    encoder_properties = EncoderProperties()
    decoder_properties = DecoderProperties()
    decoder_properties.hyd_model_net_props.flow_between_stores = args.flow_between_stores
    decoder_properties.hyd_model_net_props.store_dim = args.num_stores

    train_test_everything(1, 1, r"/cw3e/mead/projects/cwp101/scratch/hilarymcmillan/camels-us/basin_dataset_public_v1p2",
                          'models/Epoch200' if args.reload else None, 'models', data_root=r"/home/hilarymcmillan/hydro/HydroML/data",
                          encoder_properties=encoder_properties, decoder_properties=decoder_properties)
