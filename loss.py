# focal loss
# https://arxiv.org/pdf/1708.02002.pdf

import torch
import torchvision
from torch import nn
from torch.nn import functional as F

# copy from  https://github.com/clcarwin/focal_loss_pytorch
from models.BaseModels import BaseModule

use_cuda = torch.cuda.is_available()
FloatTensor = torch.cuda.FloatTensor if use_cuda else torch.FloatTensor
LongTensor = torch.cuda.LongTensor if use_cuda else torch.LongTensor
Tensor = FloatTensor


class FocalLoss(nn.Module):
    # alpha=0.75 gives the best for this project
    def __init__(self, gamma=2.0, alpha=0.25, size_average=True):
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.alpha = alpha
        if isinstance(alpha, (float, int)):
            self.alpha = torch.Tensor([alpha, 1 - alpha])

        elif isinstance(alpha, list):
            self.alpha = torch.Tensor(alpha)

        self.size_average = size_average

    def forward(self, input, target):
        if input.dim() > 2:
            input = input.view(input.size(0), input.size(1), -1)  # N,C,H,W => N,C,H*W
            input = input.transpose(1, 2)  # N,C,H*W => N,H*W,C
            input = input.contiguous().view(-1, input.size(2))  # N,H*W,C => N*H*W,C
        target = target.view(-1, 1)

        logpt = F.log_softmax(input)
        logpt = logpt.gather(1, target)
        logpt = logpt.view(-1)
        pt = logpt.data.exp()

        if self.alpha is not None:
            if self.alpha.type() != input.data.type():
                self.alpha = self.alpha.type_as(input.data)
            at = self.alpha.gather(0, target.data.view(-1))
            logpt = logpt * at

        loss = -1 * (1 - pt) ** self.gamma * logpt
        if self.size_average:
            return loss.mean()
        else:
            return loss.sum()


# +++++++++++++++++++++++++++++++++++++
#           Loss for inpainting
# -------------------------------------

class InapintingLoss(nn.Module):
    # https://github.com/naoto0804/pytorch-inpainting-with-partial-conv/blob/master/loss.py
    # Image Inpainting for Irregular Holes Using Partial Convolutions
    def __init__(self, feature_encoder, feature_range=3):
        super(InapintingLoss, self).__init__()
        self.l1 = nn.L1Loss()
        self.feature_encoder = FeatureExtractor(feature_encoder, feature_range)

    def forward(self, raw_input, mask, output, origin):
        comp_img = raw_input * mask + output * (1 - mask)
        # extra features
        feature_comp = self.feature_encoder(comp_img)
        feature_output = self.feature_encoder(output)
        feature_origin = self.feature_encoder(origin)

        loss_validate = self.l1(mask * output, mask * origin)
        loss_hole = self.l1((1 - mask) * output, (1 - mask) * origin)
        loss_total_var = total_variation_loss(comp_img)  # total variation (smoothing penalty)

        # perceptual loss
        loss_perceptual_1 = sum(map(lambda x, y: self.l1(x, y), feature_comp, feature_origin))
        loss_perceptual_2 = sum(map(lambda x, y: self.l1(x, y), feature_output, feature_origin))
        loss_perceptual = loss_perceptual_1 + loss_perceptual_2

        # style loss
        loss_style_1 = sum(map(lambda x, y: self.l1(gram_matrix(x), gram_matrix(y)),
                               feature_output, feature_origin))
        loss_style_2 = sum(map(lambda x, y: self.l1(gram_matrix(x), gram_matrix(y)),
                               feature_comp, feature_origin))
        loss_style = loss_style_1 + loss_style_2

        # weights are recommended in the paper P7
        loss = 1.0 * loss_validate + 6.0 * loss_hole + \
               0.1 * loss_total_var + 0.05 * loss_perceptual + 120 * loss_style
        return loss


class FeatureExtractor(nn.Module):
    def __init__(self, encoder, feature_range=3):
        super(FeatureExtractor, self).__init__()
        self.layers = nn.Sequential(*[encoder.features[i] for i in range(feature_range)])
        for layer in self.layers:
            for param in layer.parameters():
                param.requires_grad = False

    def forward(self, x):
        out = []
        for layer in self.layers:
            x = layer(x)
            out.append(x)
        return out


class Vgg19Extractor(BaseModule):
    def __init__(self, pretrained=True):
        super(Vgg19Extractor, self).__init__()
        vgg19 = torchvision.models.vgg16(pretrained=pretrained)
        feature1 = nn.Sequential(*vgg19.features[:5])
        feature2 = nn.Sequential(*vgg19.features[5:10])
        feature3 = nn.Sequential(*vgg19.features[10:17])
        self.features = nn.Sequential(*[feature1, feature2, feature3])
        for param in self.features.parameters():
            param.requires_grad = False

    def forward(self, img):
        result = []
        for layer in self.features.children():
            img = layer(img)
            result.append(img)
        return result


def gram_matrix(feat):
    # https://github.com/pytorch/examples/blob/master/fast_neural_style/neural_style/utils.py
    (b, ch, h, w) = feat.size()
    feat = feat.view(b, ch, h * w)
    feat_t = feat.transpose(1, 2)
    gram = torch.bmm(feat, feat_t) / (ch * h * w)
    return gram


def total_variation_loss(image):
    # shift one pixel and get difference (for both x and y direction)
    loss = torch.mean(torch.abs(image[:, :, :, :-1] - image[:, :, :, 1:])) + \
           torch.mean(torch.abs(image[:, :, :-1, :] - image[:, :, 1:, :]))
    return loss


# +++++++++++++++++++++++++++++++++++++
#           Loss for classification with LSTM
# -------------------------------------
# Multi-label Image Recognition by Recurrently Discovering Attentional Regions by Wang, chen,  Li, Xu, and Lin


class BCERegionLoss(nn.Module):
    def __init__(self):
        super(BCERegionLoss, self).__init__()
        self.anchor_box = FloatTensor([(0.4, 0.4), (0.4, -0.4), (-0.4, -0.4), (-0.4, 0.4)]).unsqueeze(-1)
        self.scale_alpha = FloatTensor([1])
        self.positive_beta = FloatTensor([0.2])
        self.bce = nn.BCEWithLogitsLoss()

    def scale_loss(self, scale):
        #         assert scale.size(1) == scale.size(2)
        sx = scale[:, 0, 0]
        ls = torch.pow(F.relu(torch.abs(sx) - self.scale_alpha), 2)
        sy = scale[:, 1, 1]
        ly = torch.pow(F.relu(torch.abs(sy) - self.scale_alpha), 2)
        positive_loss = F.relu(self.positive_beta - sx) + F.relu(self.positive_beta - sy)

        loss = 0.1 * positive_loss + ls + ly
        return loss.sum().view(1)

    def anchor_loss(self, attention_region):
        # input: num_class, 2 (anchor x, y) , 1   -self.anchor_box
        distance = 0.5 * torch.pow(attention_region - self.anchor_box, 2).sum(1)
        return 0.01 * distance.sum().view(1)

    def forward(self, input, target):
        category, transform_box = input
        #         scores, index = category.max(1)
        #         bce_loss = self.bce(scores, target)
        bce_loss = FloatTensor([0])
        for i in range(category.size(1)):
            bce_loss = bce_loss + self.bce(category[:, i, :], target)
        bce_loss = bce_loss / category.size(1)

        regions = transform_box[:, 1:, :, 2:]
        region_loss = torch.cat([self.anchor_loss(i) for i in regions]).mean()
        scales = transform_box[:, :, :, :2]
        scale_loss = torch.cat([self.scale_loss(i) for i in scales]).mean()

        # spatial transform theta matrix (batch, 2, 3)
        # sum over the second axis so that transformed regions will not be 0 padded
        boundary = torch.abs(transform_box).sum(-1)
        boundary = torch.pow(F.relu(boundary - 1), 2)
        boundary_loss = 0.5 * boundary.view(boundary.size(0), -1).sum(-1).mean()
        return bce_loss, bce_loss + 0.01 * region_loss + 0.05 * scale_loss + 0.5 * boundary_loss
