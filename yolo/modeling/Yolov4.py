import tensorflow as tf
import tensorflow.keras as ks
from typing import *

import yolo.modeling.base_model as base_model
from yolo.modeling.backbones.csp_backbone_builder import CSP_Backbone_Builder 
from yolo.modeling.model_heads._Yolov4Neck import Yolov4Neck
from yolo.modeling.model_heads._Yolov4Head import Yolov4Head
from yolo.modeling.building_blocks import YoloLayer

from yolo.utils.file_manager import download
from yolo.utils import DarkNetConverter
from yolo.utils._darknet2tf.load_weights import split_converter, load_weights_dnBackbone, load_weights_dnHead
from yolo.utils._darknet2tf.load_weights2 import load_weights_backbone, load_weights_v4head

from yolo.utils.testing_utils import prep_gpu
prep_gpu()
from yolo.dataloaders.YoloParser import YoloParser


class Yolov4(base_model.Yolo):
    def __init__(
            self,
            input_shape=[None, None, None, 3],
            model="regular",  # options {regular, spp, tiny}
            classes=80,
            backbone=None,
            neck = None, 
            head=None,
            head_filter=None,
            masks=None,
            boxes=None,
            path_scales=None,
            x_y_scales=None,
            thresh: int = 0.45,
            class_thresh: int = 0.45,
            max_boxes: int = 200,
            scale_boxes: int = 416,
            scale_mult: float = 1.0,
            use_tie_breaker: bool = True,
            policy="float32",
            **kwargs):
        super().__init__(**kwargs)

        #required inputs
        self._input_shape = input_shape
        self._classes = classes
        self._type = model
        self._encoder_decoder_split_location = None
        self._built = False
        self._custom_aspects = False

        #setting the running policy
        if type(policy) != str:
            policy = policy.name
        self._og_policy = policy
        self._policy = tf.keras.mixed_precision.experimental.global_policy(
        ).name
        self.set_policy(policy=policy)

        #filtering params
        self._thresh = thresh
        self._class_thresh = 0.45
        self._max_boxes = max_boxes
        self._scale_boxes = scale_boxes
        self._scale_mult = scale_mult
        self._x_y_scales = x_y_scales

        #init base params
        self._encoder_decoder_split_location = None
        self._boxes = boxes
        self._masks = masks
        self._path_scales = path_scales
        self._use_tie_breaker = use_tie_breaker

        #init models
        self.model_name = model
        self._model_name = None
        self._backbone_name = None
        self.backbone = backbone
        self.neck = neck
        self.head = head
        self.head_filter = head_filter

        self.get_models()
        self.parser = None
        return

    def process_datasets(self,
                         train,
                         test,
                         batch_size=1,
                         image_w=416,
                         image_h=416,
                         fixed_size=False,
                         jitter_im=0.1,
                         jitter_boxes=0.005):
        if self.parser == None:
            parser = YoloParser(image_w=image_w,
                                image_h=image_h,
                                fixed_size=fixed_size,
                                jitter_im=jitter_im,
                                jitter_boxes=jitter_boxes,
                                masks=self._masks,
                                anchors=self._boxes)
            self.parser = parser
        else:
            parser = self.parser

        preprocess_train = parser.unbatched_process_fn(is_training=True)
        postprocess_train = parser.batched_process_fn(is_training=True)

        preprocess_test = parser.unbatched_process_fn(is_training=False)
        postprocess_test = parser.batched_process_fn(is_training=False)

        train = train.map(preprocess_train).padded_batch(batch_size)
        train = train.map(postprocess_train)
        test = test.map(preprocess_test).padded_batch(batch_size)
        test = test.map(postprocess_test)

        train_size = tf.data.experimental.cardinality(train)
        test_size = tf.data.experimental.cardinality(test)
        print(train_size, test_size)

        return train, test

    def get_models(self):
        default_dict = {
            "regular": {
                "backbone": "darknet53",
                "neck": "neck",
                "head": "regular",
                "name": "yolov4"
            },
        }

        if self.model_name == "regular":
            self._encoder_decoder_split_location = 106
            self._boxes = self._boxes or [(12, 16), (19, 36), (40, 28), (36, 75),(76, 55), (72, 146), (142, 110),(192, 243), (459, 401)]
            self._masks = self._masks or {
                "1024": [6, 7, 8],
                "512": [3, 4, 5],
                "256": [0, 1, 2]
            }
            self._path_scales = self._path_scales or {
                "1024": 32,
                "512": 16,
                "256": 8
            }
            self._x_y_scales = self._x_y_scales or {"1024": 1.05, "512": 1.1, "256": 1.2}

        if self.backbone == None or isinstance(self.backbone, Dict):
            self._backbone_name = default_dict[self.model_name]["backbone"]
            if isinstance(self.backbone, Dict):
                default_dict[self.model_name]["backbone"] = self.backbone
            self.backbone = CSP_Backbone_Builder(
                name=default_dict[self.model_name]["backbone"],
                config=default_dict[self.model_name]["backbone"],
                input_shape=self._input_shape)

        else:
            self._custom_aspects = True
        
        if self.neck == None or isinstance(self.neck, Dict):
            if isinstance(self.neck, Dict):
                default_dict[self.model_name]["neck"] = self.neck
            self.neck = Yolov4Neck(
                name=default_dict[self.model_name]["neck"],
                cfg_dict=default_dict[self.model_name]["neck"],
                input_shape=self._input_shape)
        else:
            self._custom_aspects = True

        if self.head == None or isinstance(self.head, Dict):
            if isinstance(self.head, Dict):
                default_dict[self.model_name]["head"] = self.head
            self.head =  Yolov4Head(
                model=default_dict[self.model_name]["head"],
                cfg_dict=default_dict[self.model_name]["head"],
                classes=self._classes,
                boxes=len(self._boxes),
                input_shape=self._input_shape)
        else:
            self._custom_aspects = True

        if self.head_filter == None:
            self.head_filter = YoloLayer(masks=self._masks,
                                         anchors=self._boxes,
                                         thresh=self._thresh,
                                         cls_thresh=self._class_thresh,
                                         max_boxes=self._max_boxes,
                                         scale_boxes=self._scale_boxes,
                                         scale_mult=self._scale_mult,
                                         path_scale=self._path_scales)

        self._model_name = default_dict[self.model_name]["name"]
        return

    def get_summary(self):
        self.backbone.summary()
        self.neck.summary()
        self.head.summary()
        print(self.backbone.output_shape)
        print(self.neck.output_shape)
        print(self.head.output_shape)
        return

    def build(self, input_shape):
        self.backbone.build(input_shape)
        self.neck.build(self.backbone.output_shape)
        self.head.build(self.neck.output_shape)
        self.head_filter.build(self.head.output_shape)
        return

    def call(self, inputs, training=True):
        feature_maps = self.backbone(inputs)
        neck_maps = self.neck(feature_maps)
        raw_head = self.head(neck_maps)
        predictions = self.head_filter(raw_head)
        return predictions

    def train_step(self, data):
        #get the data point
        image = data["image"]
        label = data["label"]

        # computer detivative and apply gradients
        with tf.GradientTape() as tape:
            y_pred = self(image, training=True)
            loss = self.compiled_loss(label, y_pred["raw_output"])

        grads = tape.gradient(loss, self.trainable_variables)
        self.optimizer.apply_gradients(zip(grads, self.trainable_variables))

        #custom metrics
        loss_metrics = dict()
        for loss in self.compiled_loss._losses:
            loss_metrics[f"{loss._path_key}_boxes"] = loss.get_box_loss()
            loss_metrics[
                f"{loss._path_key}_classes"] = loss.get_classification_loss()
            loss_metrics[f"{loss._path_key}_avg_iou"] = loss.get_avg_iou()
            loss_metrics[
                f"{loss._path_key}_confidence"] = loss.get_confidence_loss()

        #compiled metrics
        self.compiled_metrics.update_state(label, y_pred["raw_output"])
        metrics_dict = {m.name: m.result() for m in self.metrics}
        metrics_dict.update(loss_metrics)
        return metrics_dict

    def test_step(self, data):
        #get the data point
        image = data["image"]
        label = data["label"]

        # computer detivative and apply gradients
        y_pred = self(image, training=False)
        loss = self.compiled_loss(label, y_pred["raw_output"])

        #custom metrics
        loss_metrics = dict()
        for loss in self.compiled_loss._losses:
            loss_metrics[f"{loss._path_key}_boxes"] = loss.get_box_loss()
            loss_metrics[
                f"{loss._path_key}_classes"] = loss.get_classification_loss()
            loss_metrics[f"{loss._path_key}_avg_iou"] = loss.get_avg_iou()
            loss_metrics[
                f"{loss._path_key}_confidence"] = loss.get_confidence_loss()

        #compiled metrics
        self.compiled_metrics.update_state(label, y_pred["raw_output"])
        metrics_dict = {m.name: m.result() for m in self.metrics}
        metrics_dict.update(loss_metrics)
        return metrics_dict

    def load_weights_from_dn(self,
                             dn2tf_backbone=True,
                             dn2tf_head=True,
                             config_file=None,
                             weights_file=None):
        """
        load the entire Yolov3 Model for tensorflow

        example:
            load yolo with darknet wieghts for backbone
            model = Yolov3()
            model.build(input_shape = (1, 416, 416, 3))
            model.load_weights_from_dn(dn2tf_backbone = True, dn2tf_head = True)

        to be implemented
        example:
            load custom back bone weigths

        example:
            load custom head weigths

        example:
            load back bone weigths from tensorflow (our training)

        example:
            load head weigths from tensorflow (our training)

        Args:
            dn2tf_backbone: bool, if true it will load backbone weights for yolo v3 from darknet .weights file
            dn2tf_head: bool, if true it will load head weights for yolo v3 from darknet .weights file
            config_file: str path for the location of the configuration file to use when decoding darknet weights
            weights_file: str path with the file containing the dark net weights
        """
        if dn2tf_backbone or dn2tf_neck or dn2tf_head:
            if config_file is None:
                config_file = download(self._model_name + '.cfg')
            if weights_file is None:
                weights_file = download(self._model_name + '.weights')
            list_encdec = DarkNetConverter.read(config_file, weights_file)
            encoder, neck, decoder = split_converter(
                list_encdec, self._encoder_decoder_split_location, 138)

        if dn2tf_backbone:
            #load_weights_dnBackbone(self._backbone, encoder, mtype = self._backbone_name)
            load_weights_backbone(self.backbone, encoder)

        if dn2tf_head:
            load_weights_backbone(self.neck, neck)
            load_weights_v4head(self.head, decoder)

        return


if __name__ == "__main__":
    import tensorflow_datasets as tfds
    train, info = tfds.load('coco',
                            split='train',
                            shuffle_files=False,
                            with_info=True)
    test, info = tfds.load('coco',
                           split='validation',
                           shuffle_files=False,
                           with_info=True)

    model = Yolov4(model = "regular", policy="float16")
    model.get_summary()
    model.build(model._input_shape)
    model.load_weights_from_dn()

    train, test = model.process_datasets(train, test, batch_size=20)
    loss_fn = model.generate_loss(loss_type="ciou")

    optimizer = ks.optimizers.SGD(lr=1e-3)
    model.compile(optimizer=optimizer, loss=loss_fn)
    model.evaluate(test)  #fit(train, validation_data = test)
