import torch
from torch.nn import functional as F
from detectron2.config import configurable
from mask2former.modeling.transformer_decoder.maskformer_transformer_decoder import TRANSFORMER_DECODER_REGISTRY
from mask2former.modeling.transformer_decoder.position_encoding import PositionEmbeddingSine
from mask2former_video.modeling.transformer_decoder.video_mask2former_transformer_decoder import VideoMultiScaleMaskedTransformerDecoder
import einops
import random


@TRANSFORMER_DECODER_REGISTRY.register()
class VideoMultiScaleMaskedTransformerDecoder_dvis(VideoMultiScaleMaskedTransformerDecoder):

    @configurable
    def __init__(
        self,
        in_channels,
        mask_classification=True,
        *,
        num_classes: int,
        hidden_dim: int,
        num_queries: int,
        nheads: int,
        dim_feedforward: int,
        dec_layers: int,
        pre_norm: bool,
        mask_dim: int,
        enforce_input_project: bool,
        # video related
        num_frames: int,
    ):
        super().__init__(
            in_channels=in_channels, 
            mask_classification=mask_classification,
            num_classes=num_classes,
            hidden_dim=hidden_dim,
            num_queries=num_queries,
            nheads=nheads,
            dim_feedforward=dim_feedforward,
            dec_layers=dec_layers,
            pre_norm=pre_norm,
            mask_dim=mask_dim,
            enforce_input_project=enforce_input_project,
            num_frames=num_frames,
        )

        # use 2D positional embedding
        N_steps = hidden_dim // 2
        self.pe_layer = PositionEmbeddingSine(N_steps, normalize=True)

    def forward(self, x, mask_features, mask=None):
        # x is a list of multi-scale feature
        assert len(x) == self.num_feature_levels
        src = []
        pos = []
        size_list = []

        # disable mask, it does not affect performance
        del mask

        for i in range(self.num_feature_levels):
            size_list.append(x[i].shape[-2:])
            pos.append(self.pe_layer(x[i], None).flatten(2))
            src.append(self.input_proj[i](x[i]).flatten(2) + self.level_embed.weight[i][None, :, None])

            # flatten NxCxHxW to HWxNxC
            pos[-1] = pos[-1].permute(2, 0, 1)
            src[-1] = src[-1].permute(2, 0, 1)

        _, bs, _ = src[0].shape

        # QxNxC
        query_embed = self.query_embed.weight.unsqueeze(1).repeat(1, bs, 1)
        output = self.query_feat.weight.unsqueeze(1).repeat(1, bs, 1)

        predictions_class = []
        predictions_mask = []

        # prediction heads on learnable query features
        outputs_class, outputs_mask, attn_mask = self.forward_prediction_heads(
            output,
            mask_features,
            attn_mask_target_size=size_list[0]
        )
        predictions_class.append(outputs_class)
        predictions_mask.append(outputs_mask)

        for i in range(self.num_layers):
            level_index = i % self.num_feature_levels
            attn_mask[torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])] = False
            # attention: cross-attention first
            output = self.transformer_cross_attention_layers[i](
                output, src[level_index],
                memory_mask=attn_mask,
                memory_key_padding_mask=None,  # here we do not apply masking on padded region
                pos=pos[level_index], query_pos=query_embed
            )

            output = self.transformer_self_attention_layers[i](
                output, tgt_mask=None,
                tgt_key_padding_mask=None,
                query_pos=query_embed
            )
            
            # FFN
            output = self.transformer_ffn_layers[i](
                output
            )

            outputs_class, outputs_mask, attn_mask = self.forward_prediction_heads(
                output,
                mask_features,
                attn_mask_target_size=size_list[(i + 1) % self.num_feature_levels]
            )
            predictions_class.append(outputs_class)
            predictions_mask.append(outputs_mask)

        assert len(predictions_class) == self.num_layers + 1

        # expand BT to B, T  
        bt = predictions_mask[-1].shape[0]
        bs = bt // self.num_frames if self.training else 1
        t = bt // bs
        for i in range(len(predictions_mask)):
            predictions_mask[i] = einops.rearrange(predictions_mask[i], '(b t) q h w -> b q t h w', t=t)

        for i in range(len(predictions_class)):
            predictions_class[i] = einops.rearrange(predictions_class[i], '(b t) q c -> b t q c', t=t)

        pred_embds_without_norm = einops.rearrange(output, 'q (b t) c -> b c t q', t=t)
        pred_embds = self.decoder_norm(output)
        pred_embds = einops.rearrange(pred_embds, 'q (b t) c -> b c t q', t=t)

        out = {
            'pred_logits': predictions_class[-1],
            'pred_masks': predictions_mask[-1],
            'aux_outputs': self._set_aux_loss(
                predictions_class if self.mask_classification else None, predictions_mask
            ),
            'pred_embds': pred_embds,
            'pred_embds_without_norm': pred_embds_without_norm,
            'mask_features': mask_features
        }
        
        return out

    def forward_prediction_heads(self, output, mask_features, attn_mask_target_size):
        decoder_output = self.decoder_norm(output)
        decoder_output = decoder_output.transpose(0, 1)
        outputs_class = self.class_embed(decoder_output)
        mask_embed = self.mask_embed(decoder_output)
        outputs_mask = torch.einsum("bqc,bchw->bqhw", mask_embed, mask_features)

        # NOTE: prediction is of higher-resolution
        # [B, Q, H, W] -> [B, Q, H*W] -> [B, h, Q, H*W] -> [B*h, Q, HW]
        attn_mask = F.interpolate(outputs_mask, size=attn_mask_target_size, mode="bilinear", align_corners=False)
        # must use bool type
        # If a BoolTensor is provided, positions with ``True`` are not allowed to attend while ``False`` values will be unchanged.
        attn_mask = (attn_mask.sigmoid().flatten(2).unsqueeze(1).repeat(1, self.num_heads, 1, 1).flatten(0, 1) < 0.5).bool()
        attn_mask = attn_mask.detach()

        return outputs_class, outputs_mask, attn_mask


@TRANSFORMER_DECODER_REGISTRY.register()
class VideoMultiScaleMaskedTransformerDecoder_minvis(VideoMultiScaleMaskedTransformerDecoder_dvis):

    def forward(self, x, mask_features, mask=None):
        out = super(VideoMultiScaleMaskedTransformerDecoder_minvis, self).forward(x, mask_features, mask=mask)
        del out['mask_features']
        return out


@TRANSFORMER_DECODER_REGISTRY.register()
class VideoMultiScaleMaskedTransformerDecoder_dvis_clip(VideoMultiScaleMaskedTransformerDecoder):

    @configurable
    def __init__(
            self,
            in_channels,
            mask_classification=True,
            *,
            num_classes: int,
            hidden_dim: int,
            num_queries: int,
            nheads: int,
            dim_feedforward: int,
            dec_layers: int,
            pre_norm: bool,
            mask_dim: int,
            enforce_input_project: bool,
            # video related
            num_frames: int,
    ):
        super().__init__(
            in_channels=in_channels,
            mask_classification=mask_classification,
            num_classes=num_classes,
            hidden_dim=hidden_dim,
            num_queries=num_queries,
            nheads=nheads,
            dim_feedforward=dim_feedforward,
            dec_layers=dec_layers,
            pre_norm=pre_norm,
            mask_dim=mask_dim,
            enforce_input_project=enforce_input_project,
            num_frames=num_frames,
        )

        # use 2D positional embedding
        N_steps = hidden_dim // 2
        self.pe_layer = PositionEmbeddingSine(N_steps, normalize=True)

    def forward(self, x, mask_features, mask=None, clip_size=2):
        # x is a list of multi-scale feature
        assert len(x) == self.num_feature_levels
        if self.training:
            assert self.num_frames % clip_size == 0
            # random clip size for training
            if random.random() < 0.5:
                clip_size = 1
        else:
            if x[0].shape[0] < clip_size:
                clip_size = x[0].shape[0]
            assert x[0].shape[0] % clip_size == 0

        src = []
        pos = []
        size_list = []

        # disable mask, it does not affect performance
        del mask

        for i in range(self.num_feature_levels):
            size_list.append(x[i].shape[-2:])
            pos.append(self.pe_layer(x[i], None).flatten(2))
            src.append(self.input_proj[i](x[i]).flatten(2) + self.level_embed.weight[i][None, :, None])

            # flatten NxCxHxW to HWxNxC, N = B * n_frames
            pos[-1] = pos[-1].permute(2, 0, 1)
            src[-1] = src[-1].permute(2, 0, 1)

            # reshpe to (HW * clip_size, B * n_frames / clip_size, C)
            hw, bs, c = src[-1].shape
            pos[-1] = pos[-1].reshape(hw, bs // clip_size, clip_size, c).permute(0, 2, 1, 3).flatten(0, 1)
            src[-1] = src[-1].reshape(hw, bs // clip_size, clip_size, c).permute(0, 2, 1, 3).flatten(0, 1)

        _, bs, _ = src[0].shape

        # QxNxC
        query_embed = self.query_embed.weight.unsqueeze(1).repeat(1, bs, 1)
        output = self.query_feat.weight.unsqueeze(1).repeat(1, bs, 1)

        predictions_class = []
        predictions_mask = []

        # prediction heads on learnable query features
        outputs_class, outputs_mask, attn_mask = self.forward_prediction_heads(
            output,
            mask_features,
            attn_mask_target_size=size_list[0],
            clip_size=clip_size
        )
        predictions_class.append(outputs_class)
        predictions_mask.append(outputs_mask)

        for i in range(self.num_layers):
            level_index = i % self.num_feature_levels
            attn_mask[torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])] = False
            # attention: cross-attention first
            output = self.transformer_cross_attention_layers[i](
                output, src[level_index],
                memory_mask=attn_mask,
                memory_key_padding_mask=None,  # here we do not apply masking on padded region
                pos=pos[level_index], query_pos=query_embed
            )

            output = self.transformer_self_attention_layers[i](
                output, tgt_mask=None,
                tgt_key_padding_mask=None,
                query_pos=query_embed
            )

            # FFN
            output = self.transformer_ffn_layers[i](
                output
            )

            outputs_class, outputs_mask, attn_mask = self.forward_prediction_heads(
                output,
                mask_features,
                attn_mask_target_size=size_list[(i + 1) % self.num_feature_levels],
                clip_size=clip_size
            )
            predictions_class.append(outputs_class)
            predictions_mask.append(outputs_mask)

        assert len(predictions_class) == self.num_layers + 1

        # expand BT to B, T
        bt = predictions_mask[-1].shape[0]
        bs = bt // self.num_frames if self.training else 1
        t = bt // bs
        for i in range(len(predictions_mask)):
            predictions_mask[i] = einops.rearrange(predictions_mask[i], '(b t) q h w -> b q t h w', t=t)

        for i in range(len(predictions_class)):
            predictions_class[i] = einops.rearrange(predictions_class[i], '(b t) q c -> b t q c', t=t)

        output = output.unsqueeze(2).repeat(1, 1, clip_size, 1).flatten(1, 2)
        pred_embds_without_norm = einops.rearrange(output, 'q (b t) c -> b c t q', t=t)
        pred_embds = self.decoder_norm(output)
        pred_embds = einops.rearrange(pred_embds, 'q (b t) c -> b c t q', t=t)

        out = {
            'pred_logits': predictions_class[-1],
            'pred_masks': predictions_mask[-1],
            'aux_outputs': self._set_aux_loss(
                predictions_class if self.mask_classification else None, predictions_mask
            ),
            'pred_embds': pred_embds,
            'pred_embds_without_norm': pred_embds_without_norm,
            'mask_features': mask_features,
            'clip_size': clip_size,
        }

        return out

    def forward_prediction_heads(self, output, mask_features, attn_mask_target_size, clip_size):
        # output (q, bs_clip, c)
        # expand output to (q, bs_clip * clip, c)
        output = output.unsqueeze(2).repeat(1, 1, clip_size, 1).flatten(1, 2)
        decoder_output = self.decoder_norm(output)
        decoder_output = decoder_output.transpose(0, 1)
        outputs_class = self.class_embed(decoder_output)
        mask_embed = self.mask_embed(decoder_output)
        outputs_mask = torch.einsum("bqc,bchw->bqhw", mask_embed, mask_features)

        # NOTE: prediction is of higher-resolution
        # [B, Q, H, W] -> [B, Q, H*W] -> [B, h, Q, H*W] -> [B*h, Q, HW]
        attn_mask = F.interpolate(outputs_mask, size=attn_mask_target_size, mode="bilinear", align_corners=False)
        # must use bool type
        # If a BoolTensor is provided, positions with ``True`` are not allowed to attend while ``False`` values will be unchanged.
        attn_mask = attn_mask.sigmoid().flatten(2)  # (B, Q, HW)
        B, Q, HW = attn_mask.shape
        attn_mask = attn_mask.reshape(B // clip_size, clip_size, Q, HW).permute(0, 2, 3, 1).flatten(2) # (B_clip, Q, HW * clip)
        attn_mask = (attn_mask.unsqueeze(1).repeat(1, self.num_heads, 1, 1).flatten(0, 1) < 0.5).bool()
        attn_mask = attn_mask.detach()

        return outputs_class, outputs_mask, attn_mask

@TRANSFORMER_DECODER_REGISTRY.register()
class VideoMultiScaleMaskedTransformerDecoder_minvis_clip(VideoMultiScaleMaskedTransformerDecoder_dvis_clip):

    def forward(self, x, mask_features, mask=None, clip_size=2):
        out = super(VideoMultiScaleMaskedTransformerDecoder_minvis_clip, self).forward(x, mask_features, mask=mask, clip_size=clip_size)
        del out['mask_features']
        return out