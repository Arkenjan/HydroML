import torch.nn as nn
from Util import *
import numpy as np

#Decoder
class HydModelNet(nn.Module):

    def __init__(self, encoding_dim, decoder_properties: DecoderProperties.HydModelNetProperties,
                 dataset_properties: DatasetProperties):
        super(HydModelNet, self).__init__()

        input_dim = decoder_properties.input_dim2(dataset_properties, encoding_dim)
        self.use_sigmoid = False

        self.decoder_properties = decoder_properties
        self.dataset_properties = dataset_properties

        self.flow_between_stores = decoder_properties.flow_between_stores
        #self.dropout = nn.Dropout(0.5)
        self.flownet = self.make_flow_net(decoder_properties.num_layers, input_dim, decoder_properties.hidden_dim)

        self.store_outflow_dim = self.decoder_properties.b_length()

        self.outflow_layer = self.make_outflow_layer(self.store_outflow_dim, decoder_properties)
        self.inflow_layer = self.make_inflow_layer(decoder_properties.flownet_intermediate_output_dim, self.decoder_properties.store_dim)
        self.et_layer = self.make_et_layer(decoder_properties.flownet_intermediate_output_dim)
        self.init_store_layer = nn.Linear(encoding_dim, self.decoder_properties.store_dim)

        self.inflowlog = None
        self.outflowlog = None
        self.storelog = None
        self.petlog = None

        self.weight_stores = 1

    def make_flow_net(self, num_layers, input_dim, hidden_dim):
        layers = []
        for i in range(num_layers):
            this_input_dim = input_dim if i == 0 else hidden_dim
            this_output_dim = hidden_dim if i < num_layers-1 else self.decoder_properties.flownet_intermediate_output_dim
            layers.append(nn.Linear(this_input_dim, this_output_dim))
            if self.use_sigmoid:
                layers.append(nn.Sigmoid())
            else:
                layers.append(nn.ReLU())
            if i > 0 and i < num_layers-1 and dropout_rate > 0:
                layers.append(nn.Dropout(dropout_rate))
        return nn.Sequential(*layers)

    @staticmethod
    def make_outflow_layer(output_dim, decoder_properties: DecoderProperties.HydModelNetProperties):
        layer = nn.Linear(decoder_properties.flownet_intermediate_output_dim, output_dim)
        layer.bias.data -= 5  # Make the initial values generally small
        layers = [layer, nn.Sigmoid()]  # output in 0..1
        return nn.Sequential(*layers)

    @staticmethod
    def make_inflow_layer(intermediate_output_dim, output_dim):
        layer = nn.Linear(intermediate_output_dim, output_dim)
        #layer.bias.data -= 1  # Make the initial values generally small
        layers = [layer, nn.Softmax()]  # output is a unit vector with elements in 0..1
        return nn.Sequential(*layers)

    @staticmethod
    def make_et_layer(intermediate_output_dim):
        layer = nn.Linear(intermediate_output_dim, 1)
        #layer.bias.data -= 1  # Make the initial values generally small
        layers = [layer, nn.Softplus()]  # output in 0..inf
        return nn.Sequential(*layers)

    def init_stores(self, batch_size):
        stores = torch.zeros((batch_size, self.decoder_properties.store_dim)).double()   # 0mm initialization
        return stores

    def forward(self, tuple1):  # hyd_input is t x b x i
        (datapoints, encoding) = tuple1

        rain = self.dataset_properties.get_rain(datapoints)

        timesteps = datapoints.timesteps()
        batch_size = datapoints.batch_size() # rain.shape[1]
        flows = torch.zeros([timesteps, batch_size]).double()

        stores = self.init_stores(batch_size)

        self.inflowlog = np.zeros((timesteps, stores.shape[1]))
        self.outflowlog = np.zeros((timesteps, self.store_outflow_dim))
        self.storelog = np.zeros((timesteps, stores.shape[1]))
        self.petlog = np.zeros((timesteps, 1))

        fixed_data = None

        #print_inputs('Decoder fixed_data', fixed_data)

        for t in range(timesteps):
            if fixed_data is None and type(encoding) != dict:
                fixed_data = encoding if not self.decoder_properties.decoder_include_fixed \
                    else torch.cat((torch.tensor(np.array(datapoints.signatures)),
                                    torch.tensor(np.array(datapoints.attributes)), encoding), 1)
            if type(encoding) == dict:
                if self.decoder_properties.decoder_include_fixed:
                    raise Exception("TODO append this")
                first_encoding = encoding[datapoints.gauge_id_int[0]][0]
                encoding_dim = len(first_encoding)
                fixed_data = torch.zeros([batch_size, encoding_dim])
                for b in range(batch_size):
                    encoding_id = np.random.randint(0, len(encoding[datapoints.gauge_id_int[b]]))
                    fixed_data[b, :] = encoding[datapoints.gauge_id_int[b]][encoding_id, :]
            if t == 0:
                stores = self.init_store_layer(fixed_data).exp()
                # print(f"Init stores 0: {stores[0, :]}")

            climate_input = datapoints.climate_data[:, t, :]
            if self.decoder_properties.decoder_include_stores:
                log_stores = stores.clamp(min=0.1).log()
                inputs = torch.cat((climate_input, fixed_data,
                                    log_stores*self.weight_stores), 1)  # b x i
            else:
                inputs = torch.cat((climate_input, fixed_data), 1)  # b x i

            #if i == 0:
            #    print_inputs('Decoder inputs', inputs)

            outputs = self.flownet(inputs)
            a = self.inflow_layer(outputs) # a is b x stores
            if a.min() < 0 or a.max() > 1:
                raise Exception("Relative inflow flux outside [0,1]\n" + str(a))

            self.inflowlog[t, :] = a[0, :].detach()

            et = self.et_layer(outputs)
            #corrected_rain,_ = torch.max(rain[i, :] - et, 0)  # Actually the same as a relu
            corrected_rain = nn.ReLU()(rain[:, t] - et.squeeze(1))
            #rain[i, :] = corrected_rain
            rain_distn = a * corrected_rain.unsqueeze(1)  # (b x stores) . (b x 1)
            #print('a0=' + str(a[0, :]))
            #print('rain[i, :].unsqueeze(1)=' + str(rain[i, 0]))
            #print('rain_distn=' + str(rain_distn[0, :]))
            stores = stores + rain_distn  # stores is b x s

            b = self.outflow_layer(outputs)

            self.outflowlog[t, :] = b[0, :].detach()

            if torch.max(np.isnan(b.data)) == 1:
                raise Exception("NaN in b")

            if b.min() < 0 or b.max() > 1:
                raise Exception("Relative outflow flux outside [0,1]\n" + str(b))

            num_stores = stores.shape[1]
            if self.flow_between_stores:
                for destStoreId in range(num_stores):  # Model flow from all other stores to this destination
                    b_interstore = b[:, (destStoreId*num_stores):((destStoreId+1)*num_stores)]
                    flow_between = b_interstore * stores  # b x s
                    stores = stores - flow_between
                    stores[:, destStoreId] = stores[:, destStoreId] + flow_between.sum(dim=1)

                b_escape = b[:, (-2 * num_stores):(-1 * num_stores)]
                escape = b_escape * stores
                stores = stores - escape

            b_flow = b[:, (-num_stores):]
            flow_distn = b_flow * stores

            if torch.max(np.isnan(b_flow.data)) == 1:
                raise Exception("NaN in b_flow")
            if torch.max(np.isnan(stores.data)) == 1:
                raise Exception("NaN in stores")

            if torch.max(np.isnan(flow_distn.data)) == 1:
                raise Exception("NaN in flow_distn")

            stores = stores - flow_distn

            if stores.min() < 0:
                raise Exception("Negative store\n" + str(stores))

            flows[t, :] = flow_distn.sum(1)

            self.storelog[t, :] = stores[0, :].detach()
            self.petlog[t, :] = et[0].detach()

        if flows.min() < 0:
            raise Exception("Negative flow")

        if torch.max(np.isnan(flows.data)) == 1:
            raise Exception("Negative flow")

        return flows
