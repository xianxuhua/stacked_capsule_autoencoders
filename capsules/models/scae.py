# coding=utf-8
# Copyright 2019 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Capsule autoencoder implementation."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import sonnet as snt
import tensorflow as tf
import tensorflow_probability as tfp

from capsules import capsule as _capsule
from capsules import math_ops
from capsules import plot
from capsules import probe
from capsules import tensor_ops
from capsules.data import preprocess
from capsules.models import Model
from capsules.tensor_ops import make_brodcastable

tfd = tfp.distributions


class ImageCapsule(snt.AbstractModule):
  """Capsule decoder for constellations."""

  def __init__(self, n_caps, n_caps_dims, n_votes, **capsule_kwargs):
    """Builds the module.

    Args:
      n_caps: int, number of capsules.
      n_caps_dims: int, number of capsule coordinates.
      n_votes: int, number of votes generated by each capsule.
      **capsule_kwargs: kwargs passed to capsule layer.
    """
    super(ImageCapsule, self).__init__()
    self._n_caps = n_caps
    self._n_caps_dims = n_caps_dims
    self._n_votes = n_votes
    self._capsule_kwargs = capsule_kwargs

  def _build(self, h, x, presence=None):
    """Builds the module.

    Args:
      h: Tensor of encodings of shape [B, n_enc_dims].
      x: Tensor of inputs of shape [B, n_points, n_input_dims]
      presence: Tensor of shape [B, n_points, 1] or None; if it exists, it
        indicates which input points exist.

    Returns:
      A bunch of stuff.
    """
    batch_size = int(x.shape[0])

    capsule = _capsule.CapsuleLayer(self._n_caps, self._n_caps_dims,
                                    self._n_votes, **self._capsule_kwargs)

    res = capsule(h)
    vote_shape = [batch_size, self._n_caps, self._n_votes, 6]
    res.vote = tf.reshape(res.vote[Ellipsis, :-1, :], vote_shape)

    votes, scale, vote_presence_prob = res.vote, res.scale, res.vote_presence

    likelihood = _capsule.CapsuleLikelihood(votes, scale, vote_presence_prob)
    ll_res = likelihood(x, presence)
    res.update(ll_res._asdict())

    caps_presence_prob = tf.reduce_max(
        tf.reshape(vote_presence_prob,
                   [batch_size, self._n_caps, self._n_votes]), 2)

    res.caps_presence_prob = caps_presence_prob
    return res


class ImageAutoencoder(Model):
  """Capsule autoencoder."""

  def __init__(
      self,
      primary_encoder,
      primary_decoder,
      encoder,
      decoder,
      input_key,
      label_key=None,
      n_classes=None,
      dynamic_l2_weight=0.,
      caps_ll_weight=0.,
      vote_type='soft',
      pres_type='enc',
      img_summaries=False,
      stop_grad_caps_inpt=False,
      stop_grad_caps_target=False,
      prior_sparsity_loss_type='kl',
      prior_within_example_sparsity_weight=0.,
      prior_between_example_sparsity_weight=0.,
      prior_within_example_constant=0.,
      posterior_sparsity_loss_type='kl',
      posterior_within_example_sparsity_weight=0.,
      posterior_between_example_sparsity_weight=0.,
      primary_caps_sparsity_weight=0.,
      weight_decay=0.,
      feed_templates=True,
      prep='none',
  ):

    super(ImageAutoencoder, self).__init__()
    self._primary_encoder = primary_encoder
    self._primary_decoder = primary_decoder
    self._encoder = encoder
    self._decoder = decoder
    self._input_key = input_key
    self._label_key = label_key
    self._n_classes = n_classes

    self._dynamic_l2_weight = dynamic_l2_weight
    self._caps_ll_weight = caps_ll_weight
    self._vote_type = vote_type
    self._pres_type = pres_type
    self._img_summaries = img_summaries

    self._stop_grad_caps_inpt = stop_grad_caps_inpt
    self._stop_grad_caps_target = stop_grad_caps_target
    self._prior_sparsity_loss_type = prior_sparsity_loss_type
    self._prior_within_example_sparsity_weight = prior_within_example_sparsity_weight
    self._prior_between_example_sparsity_weight = prior_between_example_sparsity_weight
    self._prior_within_example_constant = prior_within_example_constant
    self._posterior_sparsity_loss_type = posterior_sparsity_loss_type
    self._posterior_within_example_sparsity_weight = posterior_within_example_sparsity_weight
    self._posterior_between_example_sparsity_weight = posterior_between_example_sparsity_weight
    self._primary_caps_sparsity_weight = primary_caps_sparsity_weight
    self._weight_decay = weight_decay
    self._feed_templates = feed_templates

    self._prep = prep


  def _img(self, data, prep='none'):

    img = data[self._input_key]
    if prep == 'sobel':
      img = preprocess.normalized_sobel_edges(img)

    return img

  def _label(self, data):
    return data.get(self._label_key, None)

  def _build(self, data):

    input_x = self._img(data, False)
    target_x = self._img(data, prep=self._prep)
    batch_size = int(input_x.shape[0])

    primary_caps = self._primary_encoder(input_x)
    pres = primary_caps.presence

    expanded_pres = tf.expand_dims(pres, -1)
    pose = primary_caps.pose
    input_pose = tf.concat([pose, 1. - expanded_pres], -1)

    input_pres = pres
    if self._stop_grad_caps_inpt:
      input_pose = tf.stop_gradient(input_pose)
      input_pres = tf.stop_gradient(pres)

    target_pose, target_pres = pose, pres
    if self._stop_grad_caps_target:
      target_pose = tf.stop_gradient(target_pose)
      target_pres = tf.stop_gradient(target_pres)

    # skip connection from the img to the higher level capsule
    if primary_caps.feature is not None:
      input_pose = tf.concat([input_pose, primary_caps.feature], -1)

    # try to feed presence as a separate input
    # and if that works, concatenate templates to poses
    # this is necessary for set transformer
    n_templates = int(primary_caps.pose.shape[1])
    templates = self._primary_decoder.make_templates(n_templates,
                                                     primary_caps.feature)

    try:
      if self._feed_templates:
        inpt_templates = templates
        if self._stop_grad_caps_inpt:
          inpt_templates = tf.stop_gradient(inpt_templates)

        if inpt_templates.shape[0] == 1:
          inpt_templates = snt.TileByDim([0], [batch_size])(inpt_templates)
        inpt_templates = snt.BatchFlatten(2)(inpt_templates)
        pose_with_templates = tf.concat([input_pose, inpt_templates], -1)
      else:
        pose_with_templates = input_pose

      h = self._encoder(pose_with_templates, input_pres)

    except TypeError:
      h = self._encoder(input_pose)

    res = self._decoder(h, target_pose, target_pres)
    res.primary_presence = primary_caps.presence

    if self._vote_type == 'enc':
      primary_dec_vote = primary_caps.pose
    elif self._vote_type == 'soft':
      primary_dec_vote = res.soft_winner
    elif self._vote_type == 'hard':
      primary_dec_vote = res.winner
    else:
      raise ValueError('Invalid vote_type="{}"".'.format(self._vote_type))

    if self._pres_type == 'enc':
      primary_dec_pres = pres
    elif self._pres_type == 'soft':
      primary_dec_pres = res.soft_winner_pres
    elif self._pres_type == 'hard':
      primary_dec_pres = res.winner_pres
    else:
      raise ValueError('Invalid pres_type="{}"".'.format(self._pres_type))

    res.bottom_up_rec = self._primary_decoder(
        primary_caps.pose,
        primary_caps.presence,
        template_feature=primary_caps.feature,
        img_embedding=primary_caps.img_embedding)

    res.top_down_rec = self._primary_decoder(
        res.winner,
        primary_caps.presence,
        template_feature=primary_caps.feature,
        img_embedding=primary_caps.img_embedding)

    rec = self._primary_decoder(
        primary_dec_vote,
        primary_dec_pres,
        template_feature=primary_caps.feature,
        img_embedding=primary_caps.img_embedding)

    tile = snt.TileByDim([0], [res.vote.shape[1]])
    tiled_presence = tile(primary_caps.presence)

    tiled_feature = primary_caps.feature
    if tiled_feature is not None:
      tiled_feature = tile(tiled_feature)

    tiled_img_embedding = tile(primary_caps.img_embedding)

    res.top_down_per_caps_rec = self._primary_decoder(
        snt.MergeDims(0, 2)(res.vote),
        snt.MergeDims(0, 2)(res.vote_presence) * tiled_presence,
        template_feature=tiled_feature,
        img_embedding=tiled_img_embedding)

    res.templates = templates
    res.template_pres = pres
    res.used_templates = rec.transformed_templates

    res.rec_mode = rec.pdf.mode()
    res.rec_mean = rec.pdf.mean()

    res.mse_per_pixel = tf.square(target_x - res.rec_mode)
    res.mse = math_ops.flat_reduce(res.mse_per_pixel)

    res.rec_ll_per_pixel = rec.pdf.log_prob(target_x)
    res.rec_ll = math_ops.flat_reduce(res.rec_ll_per_pixel)

    n_points = int(res.posterior_mixing_probs.shape[1])
    mass_explained_by_capsule = tf.reduce_sum(res.posterior_mixing_probs, 1)

    (res.posterior_within_sparsity_loss,
     res.posterior_between_sparsity_loss) = _capsule.sparsity_loss(
         self._posterior_sparsity_loss_type,
         mass_explained_by_capsule / n_points,
         num_classes=self._n_classes)

    (res.prior_within_sparsity_loss,
     res.prior_between_sparsity_loss) = _capsule.sparsity_loss(
         self._prior_sparsity_loss_type,
         res.caps_presence_prob,
         num_classes=self._n_classes,
         within_example_constant=self._prior_within_example_constant)

    label = self._label(data)
    if label is not None:
      res.posterior_cls_xe, res.posterior_cls_acc = probe.classification_probe(
          mass_explained_by_capsule,
          label,
          self._n_classes,
          labeled=data.get('labeled', None))
      res.prior_cls_xe, res.prior_cls_acc = probe.classification_probe(
          res.caps_presence_prob,
          label,
          self._n_classes,
          labeled=data.get('labeled', None))

    res.best_cls_acc = tf.maximum(res.prior_cls_acc, res.posterior_cls_acc)

    res.primary_caps_l1 = math_ops.flat_reduce(res.primary_presence)


    if self._weight_decay > 0.0:
      decay_losses_list = []
      for var in tf.trainable_variables():
        if 'w:' in var.name or 'weights:' in var.name:
          decay_losses_list.append(tf.nn.l2_loss(var))
      res.weight_decay_loss = tf.reduce_sum(decay_losses_list)
    else:
      res.weight_decay_loss = 0.0


    return res

  def _loss(self, data, res):

    loss = (-res.rec_ll - self._caps_ll_weight * res.log_prob +
            self._dynamic_l2_weight * res.dynamic_weights_l2 +
            self._primary_caps_sparsity_weight * res.primary_caps_l1 +
            self._posterior_within_example_sparsity_weight *
            res.posterior_within_sparsity_loss -
            self._posterior_between_example_sparsity_weight *
            res.posterior_between_sparsity_loss +
            self._prior_within_example_sparsity_weight *
            res.prior_within_sparsity_loss -
            self._prior_between_example_sparsity_weight *
            res.prior_between_sparsity_loss +
            self._weight_decay * res.weight_decay_loss
           )

    try:
      loss += res.posterior_cls_xe + res.prior_cls_xe
    except AttributeError:
      pass

    return loss

  def _report(self, data, res):
    reports = super(ImageAutoencoder, self)._report(data, res)

    n_caps = self._decoder._n_caps  # pylint:disable=protected-access

    is_from_capsule = res.is_from_capsule
    ones = tf.ones_like(is_from_capsule)
    capsule_one_hot = tf.one_hot((is_from_capsule + ones),
                                 depth=n_caps + 1)[Ellipsis, 1:]

    num_per_group = tf.reduce_sum(capsule_one_hot, 1)
    num_per_group_per_batch = tf.reduce_mean(tf.to_float(num_per_group), 0)

    reports.update({
        'votes_per_capsule_{}'.format(k): v
        for k, v in enumerate(tf.unstack(num_per_group_per_batch))
    })

    label = self._label(data)


    return reports

  def _plot(self, data, res, name=None):

    img = self._img(data)
    label = self._label(data)
    if label is not None:
      label_one_hot = tf.one_hot(label, depth=self._n_classes)

    _render_activations = functools.partial(  # pylint:disable=invalid-name
        plot.render_activations,
        height=int(img.shape[1]),
        pixels_per_caps=3,
        cmap='viridis')

    mass_explained_by_capsule = tf.reduce_sum(res.posterior_mixing_probs, 1)
    normalized_mass_expplained_by_capsule = mass_explained_by_capsule / tf.reduce_max(
        mass_explained_by_capsule, -1, keepdims=True)  # pylint:disable=line-too-long

    posterior_caps_activation = _render_activations(
        normalized_mass_expplained_by_capsule)  # pylint:disable=line-too-long
    prior_caps_activation = _render_activations(res.caps_presence_prob)

    is_from_capsule = snt.BatchApply(_render_activations)(
        res.posterior_mixing_probs)

    green = res.top_down_rec
    rec_red = res.rec_mode
    rec_green = green.pdf.mode()

    flat_per_caps_rec = res.top_down_per_caps_rec.pdf.mode()
    shape = res.vote.shape[:2].concatenate(flat_per_caps_rec.shape[1:])
    per_caps_rec = tf.reshape(flat_per_caps_rec, shape)
    per_caps_rec = plot.concat_images(
        tf.unstack(per_caps_rec, axis=1), 1, vertical=False)
    one_image = tf.reduce_mean(
        self._img(data, self._prep), axis=-1, keepdims=True)
    one_rec = tf.reduce_mean(rec_red, axis=-1, keepdims=True)
    diff = tf.concat([one_image, one_rec, tf.zeros_like(one_image)], -1)

    used_templates = tf.reduce_mean(res.used_templates, axis=-1, keepdims=True)
    green_templates = tf.reduce_mean(
        green.transformed_templates, axis=-1, keepdims=True)
    templates = tf.concat(
        [used_templates, green_templates,
         tf.zeros_like(used_templates)], -1)

    templates = tf.concat(
        [templates,
         tf.ones_like(templates[:, :, :, :1]), is_from_capsule], 3)

    all_imgs = [
        img, rec_red, rec_green, diff, prior_caps_activation,
        tf.zeros_like(rec_red[:, :, :1]), posterior_caps_activation,
        per_caps_rec
    ] + list(tf.unstack(templates, axis=1))

    for i, img in enumerate(all_imgs):
      if img.shape[-1] == 1:
        all_imgs[i] = tf.image.grayscale_to_rgb(img)

    img_with_templates = plot.concat_images(all_imgs, 1, vertical=False)

    def render_corr(x, y):
      corr = abs(plot.correlation(x, y))
      rendered_corr = tf.expand_dims(_render_activations(corr), 0)
      return plot.concat_images(
          tf.unstack(rendered_corr, axis=1), 3, vertical=False)

    if label is not None:

      posterior_label_corr = render_corr(normalized_mass_expplained_by_capsule,
                                         label_one_hot)
      prior_label_corr = render_corr(res.caps_presence_prob, label_one_hot)
      label_corr = plot.concat_images([prior_label_corr, posterior_label_corr],
                                      3,
                                      vertical=True)
    else:
      label_corr = tf.zeros_like(img)

    n_examples = min(int(shape[0]), 16)
    plot_params = dict(
        img_with_templates=dict(
            grid_height=n_examples,
            zoom=3.,
        ))

    templates = res.templates
    if len(templates.shape) == 5:
      if templates.shape[0] == 1:
        templates = tf.squeeze(templates, 0)

      else:
        templates = templates[:n_examples]
        templates = plot.concat_images(
            tf.unstack(templates, axis=1), 1, vertical=False)
        plot_params['templates'] = dict(grid_height=n_examples)

    plot_dict = dict(
        templates=templates,
        img_with_templates=img_with_templates[:n_examples],
        label_corr=label_corr,
    )

    return plot_dict, plot_params


