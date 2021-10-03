import functools
import math
from math import sqrt

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from torch import einsum

from models.diffusion.unet_diffusion import AttentionBlock
from models.vqvae.vqvae import Quantize
from trainer.networks import register_model
from utils.util import opt_get


def default(val, d):
    return val if val is not None else d


def eval_decorator(fn):
    def inner(model, *args, **kwargs):
        was_training = model.training
        model.eval()
        out = fn(model, *args, **kwargs)
        model.train(was_training)
        return out
    return inner


class ChannelAttentionModule(nn.Module):
    def __init__(self, channels_in, channels_out, attention_dim, layers, num_heads=1):
        super().__init__()
        self.channels_out = channels_out

        self.positional_embeddings = nn.Embedding(channels_out, attention_dim)
        self.first_layer = nn.Conv1d(channels_in, attention_dim, kernel_size=1)
        self.layers = nn.Sequential(*[AttentionBlock(attention_dim, num_heads=num_heads) for _ in range(layers)])
        self.post_attn_layer = nn.Conv1d(attention_dim, 1, kernel_size=1)

        # This is the bypass. It performs the same computation, without attention. Stabilizes the network early on.
        self.bypass = nn.Conv1d(channels_in, channels_out, kernel_size=1)
        self.mix = nn.Parameter(torch.zeros((1,)))

    def forward(self, inp):
        # Collapse structural dimension of x
        b, c, w = inp.shape
        x = inp.permute(0,2,1).reshape(b*w, c).unsqueeze(-1).repeat(1,1,self.channels_out)
        x = self.first_layer(x)
        emb = self.positional_embeddings(torch.arange(0, self.channels_out, device=x.device)).permute(1,0).unsqueeze(0)
        x = emb + x
        x = self.layers(x)
        out = self.post_attn_layer(x).squeeze(1)
        out = out.view(b,w,self.channels_out).permute(0,2,1)

        bypass = self.bypass(inp)

        return bypass * (1-self.mix) + out * self.mix


class ResBlock(nn.Module):
    def __init__(self, chan, conv, activation):
        super().__init__()
        self.net = nn.Sequential(
            conv(chan, chan, 3, padding = 1),
            activation(),
            conv(chan, chan, 3, padding = 1),
            activation(),
            conv(chan, chan, 1)
        )

    def forward(self, x):
        return self.net(x) + x


class UpsampledConv(nn.Module):
    def __init__(self, conv, *args, **kwargs):
        super().__init__()
        assert 'stride' in kwargs.keys()
        self.stride = kwargs['stride']
        del kwargs['stride']
        self.conv = conv(*args, **kwargs)

    def forward(self, x):
        up = nn.functional.interpolate(x, scale_factor=self.stride, mode='nearest')
        return self.conv(up)


class DiscreteVAE(nn.Module):
    def __init__(
        self,
        positional_dims=2,
        num_tokens = 512,
        codebook_dim = 512,
        num_layers = 3,
        num_resnet_blocks = 0,
        hidden_dim = 64,
        channels = 3,
        stride = 2,
        kernel_size = 4,
        use_transposed_convs = True,
        encoder_norm = False,
        activation = 'relu',
        smooth_l1_loss = False,
        straight_through = False,
        normalization = None, # ((0.5,) * 3, (0.5,) * 3),
        record_codes = False,
    ):
        super().__init__()
        assert num_layers >= 1, 'number of layers must be greater than or equal to 1'
        has_resblocks = num_resnet_blocks > 0

        self.num_tokens = num_tokens
        self.num_layers = num_layers
        self.straight_through = straight_through
        self.codebook = Quantize(codebook_dim, num_tokens)
        self.positional_dims = positional_dims

        assert positional_dims > 0 and positional_dims < 3  # This VAE only supports 1d and 2d inputs for now.
        if positional_dims == 2:
            conv = nn.Conv2d
            conv_transpose = nn.ConvTranspose2d
        else:
            conv = nn.Conv1d
            conv_transpose = nn.ConvTranspose1d
        if not use_transposed_convs:
            conv_transpose = functools.partial(UpsampledConv, conv)

        if activation == 'relu':
            act = nn.ReLU
        elif activation == 'silu':
            act = nn.SiLU
        else:
            assert NotImplementedError()


        enc_chans = [hidden_dim * 2 ** i for i in range(num_layers)]
        dec_chans = list(reversed(enc_chans))

        enc_chans = [channels, *enc_chans]

        dec_init_chan = codebook_dim if not has_resblocks else dec_chans[0]
        dec_chans = [dec_init_chan, *dec_chans]

        enc_chans_io, dec_chans_io = map(lambda t: list(zip(t[:-1], t[1:])), (enc_chans, dec_chans))

        enc_layers = []
        dec_layers = []

        pad = (kernel_size - 1) // 2
        for (enc_in, enc_out), (dec_in, dec_out) in zip(enc_chans_io, dec_chans_io):
            enc_layers.append(nn.Sequential(conv(enc_in, enc_out, kernel_size, stride = stride, padding = pad), act()))
            if encoder_norm:
                enc_layers.append(nn.GroupNorm(8, enc_out))
            dec_layers.append(nn.Sequential(conv_transpose(dec_in, dec_out, kernel_size, stride = stride, padding = pad), act()))

        for _ in range(num_resnet_blocks):
            dec_layers.insert(0, ResBlock(dec_chans[1], conv, act))
            enc_layers.append(ResBlock(enc_chans[-1], conv, act))

        if num_resnet_blocks > 0:
            dec_layers.insert(0, conv(codebook_dim, dec_chans[1], 1))

        enc_layers.append(conv(enc_chans[-1], codebook_dim, 1))
        dec_layers.append(ChannelAttentionModule(dec_chans[-1], channels, hidden_dim, layers=3, num_heads=1))

        self.encoder = nn.Sequential(*enc_layers)
        self.decoder = nn.Sequential(*dec_layers)

        self.loss_fn = F.smooth_l1_loss if smooth_l1_loss else F.mse_loss

        # take care of normalization within class
        self.normalization = normalization
        self.record_codes = record_codes
        if record_codes:
            self.codes = torch.zeros((1228800,), dtype=torch.long)
            self.code_ind = 0
        self.internal_step = 0

    def norm(self, images):
        if not self.normalization is not None:
            return images

        means, stds = map(lambda t: torch.as_tensor(t).to(images), self.normalization)
        arrange = 'c -> () c () ()' if self.positional_dims == 2 else 'c -> () c ()'
        means, stds = map(lambda t: rearrange(t, arrange), (means, stds))
        images = images.clone()
        images.sub_(means).div_(stds)
        return images

    def get_debug_values(self, step, __):
        dbg = {'decoder_attn_bypass': self.decoder[-1].mix.item()}
        if self.record_codes:
            # Report annealing schedule
            dbg.update({'histogram_codes': self.codes})
        return dbg

    @torch.no_grad()
    @eval_decorator
    def get_codebook_indices(self, images):
        img = self.norm(images)
        logits = self.encoder(img).permute((0,2,3,1) if len(img.shape) == 4 else (0,2,1))
        sampled, commitment_loss, codes = self.codebook(logits)
        return codes

    def decode(
        self,
        img_seq
    ):
        image_embeds = self.codebook.embed_code(img_seq)
        b, n, d = image_embeds.shape

        kwargs = {}
        if self.positional_dims == 1:
            arrange = 'b n d -> b d n'
        else:
            h = w = int(sqrt(n))
            arrange = 'b (h w) d -> b d h w'
            kwargs = {'h': h, 'w': w}
        image_embeds = rearrange(image_embeds, arrange, **kwargs)
        images = self.decoder(image_embeds)
        return images

    def infer(self, img):
        img = self.norm(img)
        logits = self.encoder(img).permute((0,2,3,1) if len(img.shape) == 4 else (0,2,1))
        sampled, commitment_loss, codes = self.codebook(logits)
        return self.decode(codes)

    # Note: This module is not meant to be run in forward() except while training. It has special logic which performs
    # evaluation using quantized values when it detects that it is being run in eval() mode, which will be substantially
    # more lossy (but useful for determining network performance).
    def forward(
        self,
        img
    ):
        img = self.norm(img)
        logits = self.encoder(img).permute((0,2,3,1) if len(img.shape) == 4 else (0,2,1))
        sampled, commitment_loss, codes = self.codebook(logits)
        sampled = sampled.permute((0,3,1,2) if len(img.shape) == 4 else (0,2,1))

        if self.training:
            out = sampled
            for d in self.decoder:
                out = d(out)
        else:
            # This is non-differentiable, but gives a better idea of how the network is actually performing.
            out = self.decode(codes)

        # reconstruction loss
        recon_loss = self.loss_fn(img, out, reduction='none')

        # This is so we can debug the distribution of codes being learned.
        if self.record_codes and self.internal_step % 50 == 0:
            codes = codes.flatten()
            l = codes.shape[0]
            i = self.code_ind if (self.codes.shape[0] - self.code_ind) > l else self.codes.shape[0] - l
            self.codes[i:i+l] = codes.cpu()
            self.code_ind = self.code_ind + l
            if self.code_ind >= self.codes.shape[0]:
                self.code_ind = 0
        self.internal_step += 1

        return recon_loss, commitment_loss, out


@register_model
def register_dvae_channel_attention(opt_net, opt):
    return DiscreteVAE(**opt_get(opt_net, ['kwargs'], {}))


if __name__ == '__main__':
    #v = DiscreteVAE()
    #o=v(torch.randn(1,3,256,256))
    #print(o.shape)
    v = DiscreteVAE(channels=80, normalization=None, positional_dims=1, num_tokens=4096, codebook_dim=4096,
                    hidden_dim=256, stride=2, num_resnet_blocks=2, kernel_size=3, num_layers=2, use_transposed_convs=False)
    #v.eval()
    o=v(torch.randn(1,80,256))
    print(v.get_debug_values(0, 0))
    print(o[-1].shape)