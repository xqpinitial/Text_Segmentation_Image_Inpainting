# reference:
# https://github.com/tonylins/pytorch-mobilenet-v2/blob/master/MobileNetV2.py
# https://arxiv.org/pdf/1801.04381.pdf

import torch
from torch import nn
from torch.utils.checkpoint import checkpoint

from models.common import SpatialChannelSqueezeExcitation
from .BaseModels import BaseModule, Conv_block
from .partial_convolution import partial_gated_conv_block


class MobileNetV2(BaseModule):
    def __init__(self, width_mult=1, activation=nn.ReLU6(), bias=False, add_sece=False, add_partial=False, ):

        super(MobileNetV2, self).__init__()
        self.add_partial = add_partial
        self.conv_block = Conv_block if not add_partial else partial_gated_conv_block
        self.res_block = InvertedResidual if not add_partial else PartialInvertedResidual
        self.act_fn = activation
        self.bias = bias
        self.width_mult = width_mult
        self.out_stride = 32  # 1/32 of input size
        self.inverted_residual_setting = [
            # t, c, n, s, dial
            [1, 16, 1, 1, 1],
            [6, 24, 2, 2, 1],
            [6, 32, 3, 2, 1],
            [6, 64, 4, 2, 1],
            [6, 96, 3, 1, 1],
            [6, 160, 3, 2, 1],
            [6, 320, 1, 1, 1],
        ]
        self.last_channel = 0  # last one is avg pool
        self.features = self.make_inverted_resblocks(self.inverted_residual_setting, add_sece)

    def make_inverted_resblocks(self, settings, add_sece):
        in_channel = self._make_divisible(32 * self.width_mult, divisor=8)

        # first_layer
        features = [nn.Sequential(*self.conv_block(3, in_channel, kernel_size=3, stride=2,
                                                   padding=(3 - 1) // 2, bias=self.bias,
                                                   BN=True, activation=self.act_fn))]

        for t, c, n, s, d in settings:
            out_channel = self._make_divisible(c * self.width_mult, divisor=8)
            # out_channel = int(c * self.width_mult)
            block = []
            for i in range(n):
                if i == 0:
                    block.append(self.res_block(in_channel, out_channel, s, t, d,
                                                activation=self.act_fn, bias=self.bias, add_sece=add_sece))
                else:
                    block.append(self.res_block(in_channel, out_channel, 1, t, d,
                                                activation=self.act_fn, bias=self.bias, add_sece=add_sece))
                in_channel = out_channel
            features.append(nn.Sequential(*block))
        # last layer
        self.last_channel = out_channel
        return nn.Sequential(*features)

    def load_pre_train_checkpoint(self, pre_train_checkpoint, free_last_blocks):
        if pre_train_checkpoint:
            if isinstance(pre_train_checkpoint, str):
                self.load_state_dict(torch.load(pre_train_checkpoint, map_location='cpu'))
            else:
                self.load_state_dict(pre_train_checkpoint)
            print("Encoder check point is loaded")
        else:
            print("No check point for the encoder is loaded. ")
        if free_last_blocks >= 0:
            self.freeze_params(free_last_blocks)

        else:
            print("All layers in the encoders are re-trained. ")

    def freeze_params(self, free_last_blocks=2):
        # the last 4 blocks are changed from stride of 2 to dilation of 2
        for i in range(len(self.features) - free_last_blocks):
            for params in self.features[i].parameters():
                params.requires_grad = False
        print("{}/{} layers in the encoder are freezed.".format(len(self.features) - free_last_blocks,
                                                                len(self.features)))

    def _make_divisible(self, v, divisor=8, min_value=None):
        # https://github.com/tensorflow/models/blob/7367d494135368a7790df6172206a58a2a2f3d40/research/slim/nets/mobilenet/mobilenet.py#L62
        if min_value is None:
            min_value = divisor
        new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
        # Make sure that round down does not go down by more than 10%.
        if new_v < 0.9 * v:
            new_v += divisor

        return new_v

    #    for partial conv ---- will not use
    # def load_state_dict(self, state_dict, strict=True):
    #     own_state = self.state_dict()
    #     if self.add_partial:  # remove all mask conv
    #         own_name = list(filter(lambda x: 'mask_conv' not in x, list(own_state)))[:len(own_state)]
    #         state_dict = {k: v for k, v in zip(own_name, state_dict.values())}
    #     for name, param in state_dict.items():
    #         if name in own_state:
    #             try:
    #                 own_state[name].copy_(param.data)
    #             except Exception as e:
    #                 print("-----------------------------------------")
    #                 print("Parameter {} fails to load.".format(name))
    #                 print(e)
    #         else:
    #             print("Parameter {} is not in the model. ".format(name))

    def forward(self, x):
        return self.features(x)

    def forward_checkpoint(self, x):
        with self.set_activation_inplace():
            return checkpoint(self.forward, x)


class InvertedResidual(BaseModule):
    def __init__(self, in_channel, out_channel, stride, expand_ratio, dilation, conv_block_fn=Conv_block,
                 activation=nn.ReLU6(), bias=False, add_sece=False):
        super(InvertedResidual, self).__init__()
        self.conv_bloc = conv_block_fn
        self.stride = stride
        self.act_fn = activation
        self.bias = bias
        self.in_channels = in_channel
        self.out_channels = out_channel
        # assert stride in [1, 2]

        self.res_connect = self.stride == 1 and in_channel == out_channel
        self.conv = self.make_body(in_channel, out_channel, stride, expand_ratio, dilation, add_sece)

    def make_body(self, in_channel, out_channel, stride, expand_ratio, dilation, add_sece):
        # standard convolution
        mid_channel = in_channel * expand_ratio
        m = self.conv_bloc(in_channel, mid_channel,
                           1, 1, 0, bias=self.bias,
                           BN=True, activation=self.act_fn)
        # depth-wise separable convolution
        m += self.conv_bloc(mid_channel, mid_channel, 3, stride, padding=1 + (dilation - 1),
                            dilation=dilation, groups=mid_channel, bias=self.bias,
                            BN=True, activation=self.act_fn)
        # linear to preserve info : see the section: linear bottleneck. Removing the activation improves the result
        m += self.conv_bloc(mid_channel, out_channel, 1, 1, 0, bias=self.bias, BN=True, activation=None)
        if add_sece:
            m += [SpatialChannelSqueezeExcitation(out_channel, reduction=16, activation=self.act_fn)]
        return nn.Sequential(*m)

    def forward(self, x):
        if self.res_connect:
            return x + self.conv(x)
        else:
            return self.conv(x)

    def forward_checkpoint(self, x):
        with torch.no_grad():
            return self.forward(x)


class PartialInvertedResidual(InvertedResidual):
    def __init__(self, in_channel, out_channel, stride, expand_ratio, dilation, conv_block_fn=partial_gated_conv_block,
                 activation=nn.ReLU6(), bias=False):
        super(PartialInvertedResidual, self).__init__(in_channel=in_channel,
                                                      out_channel=out_channel,
                                                      stride=stride,
                                                      expand_ratio=expand_ratio,
                                                      dilation=dilation,
                                                      conv_block_fn=conv_block_fn)
        self.act_fn = activation
        self.bias = bias

    def forward(self, args):
        if self.res_connect:
            x, mask = args
            out, out_mask = self.conv((x, mask))
            out = out + x

            out_mask = out_mask + mask
            # out_mask = torch.clamp(out_mask, min=0, max=1)
            return out, out_mask
        else:
            return self.conv(args)


class DilatedMobileNetV2(MobileNetV2):
    def __init__(self, width_mult=1, activation=nn.ReLU6(), bias=False, add_sece=False, add_partial=False, ):
        super(DilatedMobileNetV2, self).__init__(width_mult=width_mult, activation=activation,
                                                 bias=bias, add_sece=add_sece, add_partial=add_partial, )
        self.add_partial = add_partial
        self.bias = bias
        self.width_mult = width_mult
        self.act_fn = activation
        self.out_stride = 8
        # # Rethinking Atrous Convolution for Semantic Image Segmentation
        self.inverted_residual_setting = [
            # t, c, n, s, dila  # input output
            [1, 16, 1, 1, 1],  # 1/2 ---> 1/2
            [6, 24, 2, 2, 1],  # 1/2 ---> 1/4
            [6, 32, 3, 2, 1],  # 1/4 ---> 1/8
            [6, 64, 4, 1, 2],  # <-- add astrous conv and keep 1/8
            [6, 96, 3, 1, 4],
            [6, 160, 3, 1, 8],
            [6, 320, 1, 1, 16],
        ]
        self.features = self.make_inverted_resblocks(self.inverted_residual_setting, add_sece=add_sece)


class MobileNetV2Classifier(BaseModule):
    def __init__(self, num_class, width_mult=1.4, add_sece=False):
        super(MobileNetV2Classifier, self).__init__()
        self.act_fn = nn.LeakyReLU(0.3, inplace=True)  # nn.SELU(inplace=True)
        self.encoder = DilatedMobileNetV2(width_mult=width_mult, activation=self.act_fn,
                                          bias=False, add_sece=add_sece, add_partial=False)

        # if width multiple is 1.4, then there are 944 channels
        cat_feat_num = sum([i[0].out_channels for i in self.encoder.features[3:]])
        self.conv_classifier = self.make_conv_classifier(cat_feat_num, num_class)
        # self.linear = nn.Sequential(nn.AlphaDropout(0.05),  # recommend by selu's authors
        #                             nn.Linear(num_class, num_class // 16),
        #                             nn.SELU(),
        #                             nn.Linear(num_class // 16, num_class))
        # if isinstance(self.act_fn, nn.SELU):
        #     self.selu_init_params()
        # else:
        #     self.initialize_weights()

    def make_conv_classifier(self, in_channel, out_channel):
        m = nn.Sequential(
            InvertedResidual(in_channel, out_channel, stride=3, expand_ratio=1, dilation=1, conv_block_fn=Conv_block,
                             activation=self.act_fn, bias=False, add_sece=False),
            InvertedResidual(out_channel, out_channel, stride=3, expand_ratio=2, dilation=1, conv_block_fn=Conv_block,
                             activation=self.act_fn, bias=False, add_sece=False),
            *Conv_block(out_channel, out_channel, kernel_size=3, padding=1,
                        groups=out_channel, BN=False, activation=self.act_fn),
            nn.Conv2d(out_channel, out_channel, kernel_size=1),
            nn.AdaptiveAvgPool2d(1))
        return m

    def forward(self, x):
        for layer in self.encoder.features[:3]:
            x = layer(x)

        feature_maps = []
        for layer in self.encoder.features[3:]:
            x = layer(x)
            feature_maps.append(x)

        # all feature maps are 1/8 of input size
        x = torch.cat(feature_maps, dim=1)
        del feature_maps
        x = self.conv_classifier(x)
        x = x.view(x.size(0), -1)
        return x
