import math
from contextlib import contextmanager

from torch import nn

try:
    from .inplace_abn import InPlaceABN

    inplace_batch_norm = True
except ImportError:
    inplace_batch_norm = False

# +++++++++++++++++++++++++++++++++++++
#           Add more functions to PyTorch's base model
# -------------------------------------


class BaseModule(nn.Module):
    def __init__(self):
        self.act_fn = None
        super(BaseModule, self).__init__()

    def selu_init_params(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d) and m.weight.requires_grad:
                m.weight.data.normal_(0.0, 1.0 / math.sqrt(m.weight.numel()))
                if m.bias is not None:
                    m.bias.data.fill_(0)
            elif isinstance(m, nn.BatchNorm2d) and m.weight.requires_grad:
                m.weight.data.fill_(1)
                m.bias.data.zero_()

            elif isinstance(m, nn.Linear) and m.weight.requires_grad:
                m.weight.data.normal_(0, 1.0 / math.sqrt(m.weight.numel()))
                m.bias.data.zero_()

    def initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d) and m.weight.requires_grad:
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='leaky_relu')
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d) and m.weight.requires_grad:
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def load_state_dict(self, state_dict, strict=True):
        own_state = self.state_dict()
        for name, param in state_dict.items():
            if name in own_state:
                try:
                    own_state[name].copy_(param.data)
                except Exception as e:
                    print("Parameter {} fails to load.".format(name))
                    print("-----------------------------------------")
                    print(e)
            else:
                print("Parameter {} is not in the model. ".format(name))

    @contextmanager
    def set_activation_inplace(self):
        if hasattr(self, 'act_fn') and hasattr(self.act_fn, 'inplace'):
            # save memory
            self.act_fn.inplace = True
            yield
            self.act_fn.inplace = False
        else:
            yield

    def total_parameters(self):
        total = sum([i.numel() for i in self.parameters()])
        trainable = sum([i.numel() for i in self.parameters() if i.requires_grad])
        print("Total parameters : {}. Trainable parameters : {}".format(total, trainable))
        return total

    def forward(self, *x):
        raise NotImplementedError


# +++++++++++++++++++++++++++++++++++++
#           Convolution Wrappers
# -------------------------------------


def Conv_block(in_channels, out_channels, kernel_size, stride=1, padding=0,
               dilation=1, groups=1, bias=True, BN=False, activation=None):
    m = [nn.Conv2d(in_channels, out_channels, kernel_size, stride,
                   padding, dilation, groups, bias)]
    if BN:
        m += activated_batch_norm(out_channels, activation, inplace_abn=inplace_batch_norm)
    if BN is False and activation is not None:
        m += [activation]
    return m


def activated_batch_norm(in_channels, activation, inplace_abn=inplace_batch_norm):
    m = []
    if inplace_abn:
        if activation:
            m.append(InPlaceABN(in_channels, activation="leaky_relu", slope=0.3))
        else:
            m.append(InPlaceABN(in_channels, activation='none'))
    else:
        m.append(nn.BatchNorm2d(in_channels))
        if activation:
            m.append(activation)
    return m
