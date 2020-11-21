import functools
import torch
import torch.nn as nn
import torch.nn.functional as F
import models.archs.srflow_orig.module_util as mutil
from models.archs.arch_util import default_init_weights, ConvGnSilu
from utils.util import opt_get


class ResidualDenseBlock(nn.Module):
    """Residual Dense Block.

    Used in RRDB block in ESRGAN.

    Args:
        mid_channels (int): Channel number of intermediate features.
        growth_channels (int): Channels for each growth.
    """

    def __init__(self, mid_channels=64, growth_channels=32):
        super(ResidualDenseBlock, self).__init__()
        for i in range(5):
            out_channels = mid_channels if i == 4 else growth_channels
            self.add_module(
                f'conv{i+1}',
                nn.Conv2d(mid_channels + i * growth_channels, out_channels, 3,
                          1, 1))
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)
        for i in range(5):
            default_init_weights(getattr(self, f'conv{i+1}'), 0.1)


    def forward(self, x):
        """Forward function.

        Args:
            x (Tensor): Input tensor with shape (n, c, h, w).

        Returns:
            Tensor: Forward results.
        """
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        # Emperically, we use 0.2 to scale the residual for better performance
        return x5 * 0.2 + x


class RRDB(nn.Module):
    """Residual in Residual Dense Block.

    Used in RRDB-Net in ESRGAN.

    Args:
        mid_channels (int): Channel number of intermediate features.
        growth_channels (int): Channels for each growth.
    """

    def __init__(self, mid_channels, growth_channels=32):
        super(RRDB, self).__init__()
        self.rdb1 = ResidualDenseBlock(mid_channels, growth_channels)
        self.rdb2 = ResidualDenseBlock(mid_channels, growth_channels)
        self.rdb3 = ResidualDenseBlock(mid_channels, growth_channels)

    def forward(self, x):
        """Forward function.

        Args:
            x (Tensor): Input tensor with shape (n, c, h, w).

        Returns:
            Tensor: Forward results.
        """
        out = self.rdb1(x)
        out = self.rdb2(out)
        out = self.rdb3(out)
        # Emperically, we use 0.2 to scale the residual for better performance
        return out * 0.2 + x


class RRDBWithBypass(nn.Module):
    """Residual in Residual Dense Block.

    Used in RRDB-Net in ESRGAN.

    Args:
        mid_channels (int): Channel number of intermediate features.
        growth_channels (int): Channels for each growth.
    """

    def __init__(self, mid_channels, growth_channels=32):
        super(RRDBWithBypass, self).__init__()
        self.rdb1 = ResidualDenseBlock(mid_channels, growth_channels)
        self.rdb2 = ResidualDenseBlock(mid_channels, growth_channels)
        self.rdb3 = ResidualDenseBlock(mid_channels, growth_channels)
        self.bypass = nn.Sequential(ConvGnSilu(mid_channels*2, mid_channels, kernel_size=3, bias=True, activation=True, norm=True),
                                    ConvGnSilu(mid_channels, mid_channels//2, kernel_size=3, bias=False, activation=True, norm=False),
                                    ConvGnSilu(mid_channels//2, 1, kernel_size=3, bias=False, activation=False, norm=False),
                                    nn.Sigmoid())

    def forward(self, x):
        """Forward function.

        Args:
            x (Tensor): Input tensor with shape (n, c, h, w).

        Returns:
            Tensor: Forward results.
        """
        out = self.rdb1(x)
        out = self.rdb2(out)
        out = self.rdb3(out)
        bypass = self.bypass(torch.cat([x, out], dim=1))
        self.bypass_map = bypass.detach().clone()
        # Empirically, we use 0.2 to scale the residual for better performance
        return out * 0.2 * bypass + x


class RRDBNet(nn.Module):
    def __init__(self, in_nc, out_nc, nf, nb, gc=32, scale=4, opt=None):
        self.opt = opt
        super(RRDBNet, self).__init__()

        bypass = opt_get(self.opt, ['networks', 'generator', 'rrdb_bypass'])
        if bypass:
            RRDB_block_f = functools.partial(RRDBWithBypass, mid_channels=nf, growth_channels=gc)
        else:
            RRDB_block_f = functools.partial(RRDB, mid_channels=nf, growth_channels=gc)
        self.scale = scale

        self.conv_first = nn.Conv2d(in_nc, nf, 3, 1, 1, bias=True)
        self.body = mutil.make_layer(RRDB_block_f, nb)
        self.conv_body = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        #### upsampling
        self.conv_up1 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.conv_up2 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        if self.scale >= 8:
            self.conv_up3 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        if self.scale >= 16:
            self.conv_up4 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        if self.scale >= 32:
            self.conv_up5 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)

        self.conv_hr = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.conv_last = nn.Conv2d(nf, out_nc, 3, 1, 1, bias=True)

        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x, get_steps=False):
        fea = self.conv_first(x)

        block_idxs = opt_get(self.opt, ['networks', 'generator','flow', 'stackRRDB', 'blocks']) or []
        block_results = {}

        for idx, m in enumerate(self.body.children()):
            fea = m(fea)
            for b in block_idxs:
                if b == idx:
                    block_results["block_{}".format(idx)] = fea

        trunk = self.conv_body(fea)

        last_lr_fea = fea + trunk

        fea_up2 = self.conv_up1(F.interpolate(last_lr_fea, scale_factor=2, mode='nearest'))
        fea = self.lrelu(fea_up2)

        fea_up4 = self.conv_up2(F.interpolate(fea, scale_factor=2, mode='nearest'))
        fea = self.lrelu(fea_up4)

        fea_up8 = None
        fea_up16 = None
        fea_up32 = None

        if self.scale >= 8:
            fea_up8 = self.conv_up3(F.interpolate(fea, scale_factor=2, mode='nearest'))
            fea = self.lrelu(fea_up8)
        if self.scale >= 16:
            fea_up16 = self.conv_up4(F.interpolate(fea, scale_factor=2, mode='nearest'))
            fea = self.lrelu(fea_up16)
        if self.scale >= 32:
            fea_up32 = self.conv_up5(F.interpolate(fea, scale_factor=2, mode='nearest'))
            fea = self.lrelu(fea_up32)

        out = self.conv_last(self.lrelu(self.conv_hr(fea)))

        if self.scale >= 4:
            results = {'last_lr_fea': last_lr_fea,
                       'fea_up1': last_lr_fea,
                       'fea_up2': fea_up2,
                       'fea_up4': fea_up4,
                       'fea_up8': fea_up8,
                       'fea_up16': fea_up16,
                       'fea_up32': fea_up32,
                       'out': out}

            fea_up0_en = opt_get(self.opt, ['networks', 'generator','flow', 'fea_up0']) or False
            if fea_up0_en:
                results['fea_up0'] = F.interpolate(last_lr_fea, scale_factor=1/2, mode='bilinear', align_corners=False, recompute_scale_factor=True)
            fea_upn1_en = opt_get(self.opt, ['networks', 'generator','flow', 'fea_up-1']) or False
            if fea_upn1_en:
                results['fea_up-1'] = F.interpolate(last_lr_fea, scale_factor=1/4, mode='bilinear', align_corners=False, recompute_scale_factor=True)
        elif self.scale == 2:
            # "Pretend" this is is 4x by shuffling around the inputs a bit.
            half = F.interpolate(last_lr_fea, scale_factor=1/2, mode='bilinear', align_corners=False, recompute_scale_factor=True)
            quarter = F.interpolate(last_lr_fea, scale_factor=1/4, mode='bilinear', align_corners=False, recompute_scale_factor=True)
            eighth = F.interpolate(last_lr_fea, scale_factor=1/8, mode='bilinear', align_corners=False, recompute_scale_factor=True)
            results = {'last_lr_fea': half,
                       'fea_up1': half,
                       'fea_up2': last_lr_fea,
                       'fea_up4': fea_up2,
                       'fea_up8': fea_up4,
                       'fea_up16': fea_up8,
                       'fea_up32': fea_up16,
                       'out': out}

            fea_up0_en = opt_get(self.opt, ['networks', 'generator','flow', 'fea_up0']) or False
            if fea_up0_en:
                results['fea_up0'] = quarter
            fea_upn1_en = opt_get(self.opt, ['networks', 'generator','flow', 'fea_up-1']) or False
            if fea_upn1_en:
                results['fea_up-1'] = eighth
        else:
            raise NotImplementedError

        if get_steps:
            for k, v in block_results.items():
                results[k] = v
            return results
        else:
            return out
