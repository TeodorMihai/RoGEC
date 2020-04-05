import tensorflow as tf
import numpy as np
import os
import tensorflow_datasets as tfds
from absl import app as absl_app

tf.compat.v1.flags.DEFINE_bool("use_tpu", False, "Use TPUs rather than plain CPUs")
tf.compat.v1.flags.DEFINE_bool("use_map", False, "")
tf.compat.v1.flags.DEFINE_string(
    "tpu", default='teodor-cotet',
    help="The Cloud TPU to use for training. This should be either the name "
    "used when creating the Cloud TPU, or a grpc://ip.address.of.tpu:8470 "
    "url.")
tf.compat.v1.flags.DEFINE_string(
    "tpu_zone", default='us-central1-f',
    help="[Optional] GCE zone where the Cloud TPU is located in. If not "
    "specified, we will attempt to automatically detect the GCE project from "
    "metadata.")
tf.compat.v1.flags.DEFINE_string(
    "gcp_project", default='rogec-271608',
    help="[Optional] Project name for the Cloud TPU-enabled project. If not "
    "specified, we will attempt to automatically detect the GCE project from "
    "metadata.")
tf.compat.v1.flags.DEFINE_integer(
    "samples", default=1024,
    help="")
tf.compat.v1.flags.DEFINE_integer(
    "batch", default=8,
    help="")

args = tf.compat.v1.flags.FLAGS

def create_model():
  return tf.keras.Sequential(
      [tf.keras.layers.Conv2D(512, 3, activation='relu', input_shape=(64, 64, 1)),
       tf.keras.layers.Flatten(),
       tf.keras.layers.Dense(64, activation='relu'),
       tf.keras.layers.Dense(10)])

def get_dataset(batch_size=200):
  datasets, info = tfds.load(name='mnist', with_info=True, as_supervised=True,
                             try_gcs=True)
  mnist_train, mnist_test = datasets['train'], datasets['test']

  def scale(image, label):
    image = tf.cast(image, tf.float32)
    image /= 255.0

    return image, label

  train_dataset = mnist_train.map(scale).shuffle(10000).batch(batch_size)
  test_dataset = mnist_test.map(scale).batch(batch_size)

  return train_dataset, test_dataset

def scale_funct(image, label):
    image /= 2.0
    return image, label

def get_custom_dataset(total_samples, batch_size):
    global args
    data = np.random.uniform(.0, 2.0, (total_samples, 64, 64, 1))
    data = tf.convert_to_tensor(data, dtype=tf.float32)

    labels = np.random.randint(10, size=(total_samples,))
    labels = tf.convert_to_tensor(labels, dtype=tf.int32)

    train_dataset = tf.data.Dataset.from_tensor_slices((data, labels))
    if args.use_map:
        train_dataset = train_dataset.map(scale_funct)
    train_dataset = train_dataset.repeat(5).batch(batch_size, drop_remainder=True)

    return train_dataset

def main(argv):
    del argv
    global args
    batch_size = args.batch
    total_samples = args.samples

    if args.use_tpu:
        resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu=args.tpu)
        tf.config.experimental_connect_to_cluster(resolver)
        # This is the TPU initialization code that has to be at the beginning.
        tf.tpu.experimental.initialize_tpu_system(resolver)
        strategy = tf.distribute.experimental.TPUStrategy(resolver)

        with strategy.scope():
            model = create_model()
            model.compile(optimizer='adam',
                            loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                            metrics=['sparse_categorical_accuracy'])
        print(model.summary())
        print(model.count_params())

        train_dataset = get_custom_dataset(total_samples, batch_size)
        model.fit(train_dataset, epochs=5, steps_per_epoch=total_samples//batch_size)
    else:
        model = create_model()
        model.compile(optimizer='adam',
                        loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                        metrics=['sparse_categorical_accuracy'])
        print(model.summary())
        print(model.count_params())
        train_dataset = get_custom_dataset(total_samples, batch_size)
        model.fit(train_dataset, epochs=5, steps_per_epoch=total_samples//batch_size)

if __name__ == "__main__":
    absl_app.run(main)