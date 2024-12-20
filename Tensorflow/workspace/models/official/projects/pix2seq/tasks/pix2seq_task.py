# Copyright 2024 The TensorFlow Authors. All Rights Reserved.
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

"""Pix2Seq detection task definition."""

from typing import Optional

from absl import logging
import tensorflow as tf, tf_keras

from official.common import dataset_fn
from official.core import base_task
from official.core import task_factory
from official.projects.pix2seq import utils
from official.projects.pix2seq.configs import pix2seq as pix2seq_cfg
from official.projects.pix2seq.dataloaders import pix2seq_input
from official.projects.pix2seq.modeling import pix2seq_model
from official.projects.uvit.modeling import vit  # pylint: disable=unused-import
from official.vision.dataloaders import input_reader_factory
from official.vision.dataloaders import tf_example_decoder
from official.vision.dataloaders import tfds_factory
from official.vision.dataloaders import tf_example_label_map_decoder
from official.vision.evaluation import coco_evaluator
from official.vision.modeling import backbones as backbones_lib


@task_factory.register_task_cls(pix2seq_cfg.Pix2SeqTask)
class Pix2SeqTask(base_task.Task):
  """A single-replica view of training procedure.

  Pix2Seq task provides artifacts for training/evalution procedures, including
  loading/iterating over Datasets, initializing the model, calculating the loss,
  post-processing, and customized metrics with reduction.
  """

  def _build_backbones_and_endpoint_names(
      self,
  ) -> tuple[list[tf_keras.Model], list[str]]:
    """Build backbones and returns their corresponding endpoint names."""
    config: pix2seq_cfg.Pix2Seq = self._task_config.model
    input_specs = tf_keras.layers.InputSpec(
        shape=[None] + config.input_size
    )
    backbones = []
    endpoint_names = []
    for backbone_config in config.backbones:
      backbone = backbones_lib.factory.build_backbone(
          input_specs=input_specs,
          backbone_config=backbone_config.backbone,
          norm_activation_config=backbone_config.norm_activation,
      )
      backbone.trainable = not backbone_config.freeze
      backbones.append(backbone)
      endpoint_names.append(backbone_config.endpoint_name)
    return backbones, endpoint_names

  def build_model(self):
    """Build Pix2Seq model."""
    config: pix2seq_cfg.Pix2Seq = self._task_config.model
    backbones, endpoint_names = self._build_backbones_and_endpoint_names()
    model = pix2seq_model.Pix2Seq(
        backbones=backbones,
        backbone_endpoint_name=endpoint_names,
        max_seq_len=config.max_num_instances * 5,
        vocab_size=config.vocab_size,
        hidden_size=config.hidden_size,
        num_encoder_layers=config.num_encoder_layers,
        num_decoder_layers=config.num_decoder_layers,
        drop_path=config.drop_path,
        encoded_feature_dropout_rates=config.encoded_feature_dropout_rates,
        drop_units=config.drop_units,
        drop_att=config.drop_att,
        num_heads=config.num_heads,
        temperature=config.temperature,
        top_p=config.top_p,
        top_k=config.top_k,
        early_stopping_token=config.early_stopping_token,
    )
    return model

  def _get_ckpt(self, ckpt_dir_or_file: str) -> str:
    if tf.io.gfile.isdir(ckpt_dir_or_file):
      return tf.train.latest_checkpoint(ckpt_dir_or_file)
    return ckpt_dir_or_file

  def initialize(self, model: tf_keras.Model):
    """Loading pretrained checkpoint."""
    if self._task_config.init_checkpoint_modules == 'backbone':
      raise ValueError(
          'init_checkpoint_modules=backbone is deprecated. Specify backbone '
          'checkpoints in each backbone config.'
      )

    if self._task_config.init_checkpoint_modules not in ['all', 'partial', '']:
      raise ValueError(
          'Unsupported init_checkpoint_modules: '
          f'{self._task_config.init_checkpoint_modules}'
      )

    if self._task_config.init_checkpoint and any(
        [b.init_checkpoint for b in self._task_config.model.backbones]
    ):
      raise ValueError(
          'A global init_checkpoint and a backbone init_checkpoint cannot be'
          ' specified at the same time.'
      )

    if self._task_config.init_checkpoint:
      global_ckpt_file = self._get_ckpt(self._task_config.init_checkpoint)
      ckpt = tf.train.Checkpoint(**model.checkpoint_items)
      status = ckpt.restore(global_ckpt_file).expect_partial()
      if self._task_config.init_checkpoint_modules != 'partial':
        status.assert_existing_objects_matched()
      logging.info(
          'Finished loading pretrained checkpoint from %s', global_ckpt_file
      )
    else:
      # This case means that no global checkpoint was provided. Possibly,
      # backbone-specific checkpoints were.
      for backbone_config, backbone in zip(
          self._task_config.model.backbones, model.backbones
      ):
        if not backbone_config.init_checkpoint:
          continue

        backbone_init_ckpt = self._get_ckpt(backbone_config.init_checkpoint)
        if backbone_config.backbone.type == 'uvit':
          # The UVit object has a special function called load_checkpoint.
          # The other backbones do not.
          backbone.load_checkpoint(ckpt_filepath=backbone_init_ckpt)
        else:
          ckpt = tf.train.Checkpoint(backbone=backbone)
          status = ckpt.restore(backbone_init_ckpt)
          status.expect_partial().assert_existing_objects_matched()

        logging.info(
            'Finished loading pretrained backbone from %s', backbone_init_ckpt
        )

  def build_inputs(
      self, params, input_context: Optional[tf.distribute.InputContext] = None
  ):
    """Build input dataset."""

    if params.tfds_name:
      decoder = tfds_factory.get_detection_decoder(params.tfds_name)
    else:
      decoder_cfg = params.decoder.get()
      if params.decoder.type == 'simple_decoder':
        decoder = tf_example_decoder.TfExampleDecoder(
            regenerate_source_id=decoder_cfg.regenerate_source_id
        )
      elif params.decoder.type == 'label_map_decoder':
        decoder = tf_example_label_map_decoder.TfExampleDecoderLabelMap(
            label_map=decoder_cfg.label_map,
            regenerate_source_id=decoder_cfg.regenerate_source_id,
        )
      else:
        raise ValueError(
            'Unknown decoder type: {}!'.format(params.decoder.type)
        )

    parser = pix2seq_input.Parser(
        eos_token_weight=self._task_config.losses.eos_token_weight,
        output_size=self._task_config.model.input_size[:2],
        max_num_boxes=self._task_config.model.max_num_instances,
        coord_vocab_shift=self._task_config.coord_vocab_shift,
        quantization_bins=self._task_config.quantization_bins,
        aug_scale_min=params.aug_scale_min,
        aug_scale_max=params.aug_scale_max,
        aug_color_jitter_strength=params.aug_color_jitter_strength,
        label_shift=params.label_shift,
    )

    reader = input_reader_factory.input_reader_generator(
        params,
        dataset_fn=dataset_fn.pick_dataset_fn(params.file_type),
        decoder_fn=decoder.decode,
        parser_fn=parser.parse_fn(params.is_training),
    )
    dataset = reader.read(input_context=input_context)

    return dataset

  def build_losses(self, outputs, labels, aux_losses=None):
    """Builds DETR losses."""
    targets = labels['targets']
    weights = labels['weights']

    targets = tf.one_hot(targets, self._task_config.model.vocab_size)

    loss = tf_keras.losses.CategoricalCrossentropy(
        from_logits=True, reduction=tf_keras.losses.Reduction.NONE
    )(targets, outputs)

    weights = tf.cast(weights, loss.dtype)
    loss = tf.reduce_sum(loss * weights) / tf.reduce_sum(weights)

    aux_losses = tf.add_n(aux_losses) if aux_losses else 0.0

    total_loss = loss + aux_losses
    return total_loss

  def build_metrics(self, training=True):
    """Builds detection metrics."""
    metrics = []
    metric_names = ['loss']
    for name in metric_names:
      metrics.append(tf_keras.metrics.Mean(name, dtype=tf.float32))

    if not training:
      self.coco_metric = coco_evaluator.COCOEvaluator(
          annotation_file=self._task_config.annotation_file,
          include_mask=False,
          need_rescale_bboxes=False,
          per_category_metrics=self._task_config.per_category_metrics,
      )
    return metrics

  def train_step(self, inputs, model, optimizer, metrics=None):
    """Does forward and backward.

    Args:
      inputs: a dictionary of input tensors.
      model: the model, forward pass definition.
      optimizer: the optimizer for this training step.
      metrics: a nested structure of metrics objects.

    Returns:
      A dictionary of logs.
    """
    features, labels = inputs
    num_replicas = tf.distribute.get_strategy().num_replicas_in_sync

    with tf.GradientTape() as tape:
      _, outputs = model(features, labels['inputs'], training=True)
      outputs = tf.nest.map_structure(lambda x: tf.cast(x, tf.float32), outputs)

      loss = self.build_losses(
          outputs=outputs, labels=labels, aux_losses=model.losses
      )
      scaled_loss = loss / num_replicas

      # For mixed_precision policy, when LossScaleOptimizer is used, loss is
      # scaled for numerical stability.
      if isinstance(optimizer, tf_keras.mixed_precision.LossScaleOptimizer):
        scaled_loss = optimizer.get_scaled_loss(scaled_loss)

    tvars = model.trainable_variables
    grads = tape.gradient(scaled_loss, tvars)
    # Scales back gradient when LossScaleOptimizer is used.
    if isinstance(optimizer, tf_keras.mixed_precision.LossScaleOptimizer):
      grads = optimizer.get_unscaled_gradients(grads)
    optimizer.apply_gradients(list(zip(grads, tvars)))

    # Trainer class handles loss metric for you.
    logs = {self.loss: loss}

    all_losses = {
        'loss': loss,
    }

    # Metric results will be added to logs for you.
    if metrics:
      for m in metrics:
        m.update_state(all_losses[m.name])
    return logs

  def validation_step(self, inputs, model, metrics=None):
    """Validatation step.

    Args:
      inputs: a dictionary of input tensors.
      model: the keras.Model.
      metrics: a nested structure of metrics objects.

    Returns:
      A dictionary of logs.
    """
    features, labels = inputs

    tokens, logits = model(features, labels['prompt'], training=False)
    # loss = self.build_losses(
    #    outputs=outputs, labels=labels, aux_losses=model.losses)
    loss = 0.0

    # Multiply for logging.
    # Since we expect the gradient replica sum to happen in the optimizer,
    # the loss is scaled with global num_boxes and weights.
    # To have it more interpretable/comparable we scale it back when logging.
    num_replicas_in_sync = tf.distribute.get_strategy().num_replicas_in_sync
    loss *= num_replicas_in_sync

    # Evaluator class handles loss metric for you.
    logs = {self.loss: loss}

    outputs = utils.decode_object_seq_to_bbox(
        logits,
        tokens,
        self._task_config.quantization_bins,
        self._task_config.coord_vocab_shift,
    )
    pred_classes, pred_bboxes, scores, pred_num = outputs

    image_size = features.shape[1:3].as_list()
    # scale points to original image size during eval.
    scale = utils.tf_float32(image_size)[tf.newaxis, :] / utils.tf_float32(
        labels['image_info'][:, 1:2, :]
    )
    scale = scale * utils.tf_float32(labels['image_info'][:, 0:1, :])
    pred_bboxes = utils.scale_points(pred_bboxes, scale)

    predictions = {
        'detection_boxes': pred_bboxes,
        'detection_scores': scores,
        'detection_classes': pred_classes,
        'num_detections': pred_num,
        'source_id': labels['id'],
        'image_info': labels['image_info'],
    }

    ground_truths = {
        'source_id': labels['id'],
        'height': labels['image_info'][:, 0:1, 0],
        'width': labels['image_info'][:, 0:1, 1],
        'num_detections': tf.reduce_sum(
            tf.cast(tf.math.greater(labels['classes'], 0), tf.int32), axis=-1
        ),
        'boxes': labels['gt_boxes'],
        'classes': labels['classes'],
        'is_crowds': labels['is_crowd'],
    }
    logs.update({'predictions': predictions, 'ground_truths': ground_truths})

    all_losses = {
        'loss': loss,
    }

    # Metric results will be added to logs for you.
    if metrics:
      for m in metrics:
        m.update_state(all_losses[m.name])
    return logs

  def aggregate_logs(self, state=None, step_outputs=None):
    if state is None:
      self.coco_metric.reset_states()
      state = self.coco_metric

    state.update_state(
        step_outputs['ground_truths'], step_outputs['predictions']
    )
    return state

  def reduce_aggregated_logs(self, aggregated_logs, global_step=None):
    return aggregated_logs.result()
