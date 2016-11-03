"""
Train support-based models.
"""
from __future__ import print_function
from __future__ import division
from __future__ import unicode_literals

import numpy as np
import tensorflow as tf
import sys 
import time
from keras.layers import Input
from keras import backend as K
from deepchem.models import Model
from deepchem.data import pad_batch
from deepchem.data import NumpyDataset
from deepchem.metrics import to_one_hot
from deepchem.models.tf_keras_models.graph_topology import merge_dicts
from deepchem.models.tensorflow_models import model_ops
from deepchem.data import SupportGenerator
from deepchem.data import EpisodeGenerator
from deepchem.data import get_task_dataset
from deepchem.data import get_single_task_test
from deepchem.data import get_task_dataset_minus_support

class SupportGraphClassifier(Model):
  def __init__(self, sess, model,
               test_batch_size=10, support_batch_size=10,
               learning_rate=.001, decay_steps=20, decay_rate=1.,
               similarity="cosine", **kwargs):
    """Builds a support-based classifier.

    See https://arxiv.org/pdf/1606.04080v1.pdf for definition of support.

    Parameters
    ----------
    sess: tf.Session
      Session for this model
    model: SequentialSupportModel 
      Contains core layers in model. 
    n_pos: int
      Number of positive examples in support.
    n_neg: int
      Number of negative examples in support.
    decay_steps: int, optional
      Corresponds to argument decay_steps in tf.train.exponential_decay
    decay_rate: float, optional
      Corresponds to argument decay_rate in tf.train.exponential_decay
    """
    self.sess = sess
    self.similarity = similarity
    self.model = model  
    self.test_batch_size = test_batch_size
    self.support_batch_size = support_batch_size

    self.learning_rate = learning_rate
    self.decay_steps = decay_steps
    self.decay_rate = decay_rate
    self.epsilon = K.epsilon()

    self.add_placeholders()
    self.pred_op, self.scores_op, self.loss_op = self.add_training_loss()
    # Get train function
    self.train_op = self.get_training_op(self.loss_op)

    # Initialize
    self.init_fn = tf.initialize_all_variables()
    sess.run(self.init_fn)  

  def get_training_op(self, loss):
    """Attaches an optimizer to the graph."""
    ################################################################# DEBUG
    global_step = tf.Variable(0, trainable=False)
    learning_rate = tf.train.exponential_decay(
        self.learning_rate, global_step,
        self.decay_steps, self.decay_rate, staircase=True)
    opt = tf.train.AdamOptimizer(learning_rate)
    # Get train function
    return opt.minimize(self.loss_op, name="train", global_step=global_step)
    #opt = tf.train.AdamOptimizer(self.learning_rate)
    #return opt.minimize(self.loss_op, name="train")
    ################################################################# DEBUG

  def add_placeholders(self):
    """Adds placeholders to graph."""
    self.test_label_placeholder = Input(
        #tensor=K.placeholder(shape=(self.test_batch_size), dtype='float32',
        tensor=K.placeholder(shape=(self.test_batch_size), dtype='float32',
        name="label_placeholder"))
    self.test_weight_placeholder = Input(
        #tensor=K.placeholder(shape=(self.test_batch_size), dtype='float32',
        tensor=K.placeholder(shape=(self.test_batch_size), dtype='float32',
        name="weight_placeholder"))

    # TODO(rbharath): There should be weights for the support being used! 
    # Support labels
    self.support_label_placeholder = Input(
        tensor=K.placeholder(shape=[self.support_batch_size], dtype='float32',
        name="support_label_placeholder"))

  def construct_feed_dict(self, test, support, training=True, add_phase=False):
    """Constructs tensorflow feed from test/support sets."""
    # Generate dictionary elements for support 
    feed_dict = (
        self.model.support_graph_topology.batch_to_feed_dict(support.X))
    feed_dict[self.support_label_placeholder] = np.squeeze(support.y)
    # Get graph information for test 
    batch_topo_dict = (
        self.model.test_graph_topology.batch_to_feed_dict(test.X))
    feed_dict = merge_dicts([batch_topo_dict, feed_dict])
    # Generate dictionary elements for test
    feed_dict[self.test_label_placeholder] = np.squeeze(test.y)
    feed_dict[self.test_weight_placeholder] = np.squeeze(test.w)

    # Get information for keras 
    if add_phase:
      feed_dict[K.learning_phase()] = training
    return feed_dict

  def old_fit(self, dataset, n_trials=1000, n_steps_per_trial=1, n_pos=1,
          n_neg=9, log_every_n_samples=10, replace=True, **kwargs):
    """Fits model on dataset.

    Note that fitting for support models is quite different from fitting for
    other deep models. Fitting is a two-level process.  We perform n_trials,
    where for each trial, we randomply sample a support set for each given
    task, and independently a test set from that same task. The
    SupportGenerator class iterates over the tasks in random order.

    Parameters
    ----------
    dataset: dc.data.Dataset
      Dataset to fit model on.
    n_trials: int, optional
      Number of (support, test) pairs to sample and train on.
    n_steps_per_trial: int, optional
      Number of gradient descent steps to take per support.
    n_pos: int, optional
      Number of positive examples per support.
    n_neg: int, optional
      Number of negative examples per support.
    log_every_n_samples: int, optional
      Displays info every this number of samples
    replace: bool, optional
      Whether or not to use replacement when sampling supports/tests.
    """
    time_start = time.time()
    # Perform the optimization
    n_tasks = len(dataset.get_task_names())

    feed_total, run_total, test_total = 0, 0, 0
    # Create different support sets
    support_generator = SupportGenerator(dataset, range(n_tasks),
        n_pos, n_neg, n_trials, replace)
    recent_losses = []
    for ind, (task, support) in enumerate(support_generator):
      if ind % log_every_n_samples == 0:
        print("Sample %d from task %s" % (ind, str(task)))
      # Get batch to try it out on
      test_start = time.time()
      test = get_single_task_test(dataset, self.test_batch_size, task, replace)
      test_end = time.time()
      test_total += (test_end - test_start)
      feed_start = time.time()
      feed_dict = self.construct_feed_dict(test, support)
      feed_end = time.time()
      feed_total += (feed_end - feed_start)
      for step in range(n_steps_per_trial):
        # Train on support set, batch pair
        ############################################################## DEBUG
        run_start = time.time()
        _, loss = self.sess.run([self.train_op, self.loss_op], feed_dict=feed_dict)
        run_end = time.time()
        run_total += (run_end - run_start)
        if ind % log_every_n_samples == 0:
          mean_loss = np.mean(np.array(recent_losses))
          print("\tmean loss is %s" % str(mean_loss))
          recent_losses = []
        else:
          recent_losses.append(loss)
        ############################################################## DEBUG
    time_end = time.time()
    print("old_fit took %s seconds" % str(time_end-time_start))
    print("test_total: %s" % str(test_total))
    print("feed_total: %s" % str(feed_total))
    print("run_total: %s" % str(run_total))

  def fit(self, dataset, n_episodes_per_epoch=1000, nb_epochs=1, n_pos=1, n_neg=9,
          log_every_n_samples=10, **kwargs):
    """Fits model on dataset using cached supports.

    For each epcoh, sample n_episodes_per_epoch (support, test) pairs and does
    gradient descent.

    Parameters
    ----------
    dataset: dc.data.Dataset
      Dataset to fit model on.
    nb_epochs: int, optional
      number of epochs of training.
    n_episodes_per_epoch: int, optional
      Number of (support, test) pairs to sample and train on per epoch.
    n_pos: int, optional
      Number of positive examples per support.
    n_neg: int, optional
      Number of negative examples per support.
    log_every_n_samples: int, optional
      Displays info every this number of samples
    """
    time_start = time.time()
    # Perform the optimization
    n_tasks = len(dataset.get_task_names())
    n_test = self.test_batch_size

    feed_total, run_total = 0, 0
    for epoch in range(nb_epochs):
      # Create different support sets
      episode_generator = EpisodeGenerator(dataset,
          n_pos, n_neg, n_test, n_episodes_per_epoch)
      recent_losses = []
      for ind, (task, support, test) in enumerate(episode_generator):
        if ind % log_every_n_samples == 0:
          print("Epoch %d, Sample %d from task %s" % (epoch, ind, str(task)))
        # Get batch to try it out on
        feed_start = time.time()
        feed_dict = self.construct_feed_dict(test, support)
        feed_end = time.time()
        feed_total += (feed_end - feed_start)
        # Train on support set, batch pair
        run_start = time.time()
        _, loss = self.sess.run([self.train_op, self.loss_op], feed_dict=feed_dict)
        run_end = time.time()
        run_total += (run_end - run_start)
        if ind % log_every_n_samples == 0:
          mean_loss = np.mean(np.array(recent_losses))
          print("\tmean loss is %s" % str(mean_loss))
          recent_losses = []
        else:
          recent_losses.append(loss)
    time_end = time.time()
    print("fit took %s seconds" % str(time_end-time_start))
    print("feed_total: %s" % str(feed_total))
    print("run_total: %s" % str(run_total))


  def save(self):
    """Save all models

    TODO(rbharath): Saving is not yet supported for this model.
    """
    pass

  def add_training_loss(self):
    """Adds training loss and scores for network."""
    pred, scores = self.get_scores()
    losses = tf.nn.sigmoid_cross_entropy_with_logits(
        scores, self.test_label_placeholder)
    weighted_losses = tf.mul(losses, self.test_weight_placeholder)
    loss = tf.reduce_sum(weighted_losses)

    return pred, scores, loss

  def get_scores(self):
    """Adds tensor operations for computing scores.

    Computes prediction yhat (eqn (1) in Matching networks) of class for test
    compounds.
    """
    # Get featurization for test 
    # Shape (n_test, n_feat)
    test_feat = self.model.get_test_output()  
    # Get featurization for support
    # Shape (n_support, n_feat)
    support_feat = self.model.get_support_output()  

    # Computes the inner part c() of the kernel
    # (the inset equation in section 2.1.1 of Matching networks paper). 
    # Normalize
    if self.similarity == 'cosine':
      g = model_ops.cosine_distances(test_feat, support_feat)
    elif self.similarity == 'euclidean':
      g = model_ops.euclidean_distance(test_feat, support_feat)
    # Note that gram matrix g has shape (n_test, n_support)

    # soft corresponds to a(xhat, x_i) in eqn (1) of Matching Networks paper 
    # https://arxiv.org/pdf/1606.04080v1.pdf
    # Computes softmax across axis 1, (so sums distances to support set for
    # each test entry) to get attention vector
    # Shape (n_test, n_support)
    attention = tf.nn.softmax(g)  # Renormalize

    # Weighted sum of support labels
    # Shape (n_support, 1)
    support_labels = tf.expand_dims(self.support_label_placeholder, 1)
    # pred is yhat in eqn (1) of Matching Networks.
    # Shape squeeze((n_test, n_support) * (n_support, 1)) = (n_test,)
    pred = tf.squeeze(tf.matmul(attention, support_labels), [1])

    # Clip softmax probabilities to range [epsilon, 1-epsilon]
    # Shape (n_test,)
    pred = tf.clip_by_value(pred, K.epsilon(), 1.-K.epsilon())

    # Convert to logit space using inverse sigmoid (logit) function
    # logit function: log(pred) - log(1-pred)
    # Used to invoke tf.nn.sigmoid_cross_entropy_with_logits
    # in Cross Entropy calculation.
    # Shape (n_test,)
    scores = tf.log(pred) - tf.log(tf.constant(1., dtype=tf.float32)-pred)

    return pred, scores

  def predict(self, support, test):
    """Makes predictions on test given support.

    TODO(rbharath): Does not currently support any transforms.
    TODO(rbharath): Only for 1 task at a time currently. Is there a better way?
    """
    y_preds = []
    for (X_batch, y_batch, w_batch, ids_batch) in test.iterbatches(
        self.test_batch_size, deterministic=True):
      test_batch = NumpyDataset(X_batch, y_batch, w_batch, ids_batch)
      y_pred_batch = self.predict_on_batch(support, test_batch)
      y_preds.append(y_pred_batch)
    y_pred = np.concatenate(y_preds)
    return y_pred

  def predict_proba(self, support, test):
    """Makes predictions on test given support.

    TODO(rbharath): Does not currently support any transforms.
    TODO(rbharath): Only for 1 task at a time currently. Is there a better way?
    Parameters
    ----------
    support: dc.data.Dataset
      The support dataset
    test: dc.data.Dataset
      The test dataset
    """
    y_preds = []
    for (X_batch, y_batch, w_batch, ids_batch) in test.iterbatches(
        self.test_batch_size, deterministic=True):
      test_batch = NumpyDataset(X_batch, y_batch, w_batch, ids_batch)
      y_pred_batch = self.predict_proba_on_batch(support, test_batch)
      y_preds.append(y_pred_batch)
    y_pred = np.concatenate(y_preds)
    return y_pred

  def predict_on_batch(self, support, test_batch):
    """Make predictions on batch of data."""
    n_samples = len(test_batch)
    padded_test_batch = NumpyDataset(*pad_batch(
        self.test_batch_size, test_batch.X, test_batch.y, test_batch.w,
        test_batch.ids))
    feed_dict = self.construct_feed_dict(padded_test_batch, support)
    # Get scores
    pred, scores = self.sess.run([self.pred_op, self.scores_op], feed_dict=feed_dict)
    y_pred_batch = np.round(scores)
    ########################################################### DEBUG
    # Remove padded elements
    y_pred_batch = y_pred_batch[:n_samples]
    ########################################################### DEBUG
    return y_pred_batch

  def predict_proba_on_batch(self, support, test_batch):
    """Make predictions on batch of data."""
    n_samples = len(test_batch)
    padded_test_batch = NumpyDataset(*pad_batch(
        self.test_batch_size, test_batch.X, test_batch.y, test_batch.w,
        test_batch.ids))
    feed_dict = self.construct_feed_dict(padded_test_batch, support)
    # Get scores
    pred, scores = self.sess.run([self.pred_op, self.scores_op], feed_dict=feed_dict)
    y_pred_batch = to_one_hot(np.round(pred))
    ########################################################### DEBUG
    # Remove padded elements
    y_pred_batch = y_pred_batch[:n_samples]
    ########################################################### DEBUG
    return y_pred_batch
    
  def evaluate(self, dataset, metric, n_pos=1,
               n_neg=9, n_trials=1000, exclude_support=True, replace=True):
    """Evaluate performance on dataset according to metrics


    Evaluates the performance of the trained model by sampling supports randomly
    for each task in dataset. For each sampled support, the accuracy of the
    model with support provided is computed on all data for that task. If
    exclude_support is True (by default), the support set is excluded from this
    accuracy calculation. exclude_support should be set to false if model's
    memorization capacity wants to be evaluated. 
    

    Since the accuracy on a task is dependent on the choice of random support,
    the evaluation experiment is repeated n_trials times for each task.
    (Each task gets n_trials experiments). The computed accuracies
    are averaged across trials.

    TODO(rbharath): Currently does not support any transformers.

    Parameters
    ----------
    dataset: dc.data.Dataset
      Dataset to test on.
    metrics: dc.metrics.Metric
      Evaluation metric.
    n_pos: int, optional
      Number of positive samples per support.
    n_neg: int, optional
      Number of negative samples per support.
    exclude_support: bool, optional
      Whether support set should be excluded when computing model accuracy.
    replace: bool, optional
      Whether or not to use replacement when sampling supports.
    """
    # Get batches
    test_tasks = range(len(dataset.get_task_names()))
    task_scores = {task: [] for task in test_tasks}
    support_generator = SupportGenerator(dataset, test_tasks,
        n_pos, n_neg, n_trials, replace)
    for ind, (task, support) in enumerate(support_generator):
      print("Eval sample %d from task %s" % (ind, str(task)))
      # TODO(rbharath): Add test for get_task_dataset_minus_support for
      # multitask case with missing data...
      if exclude_support:
        print("Removing support datapoints for eval.")
        task_dataset = get_task_dataset_minus_support(dataset, support, task)
      else:
        print("Keeping support datapoints for eval.")
        task_dataset = get_task_dataset(dataset, task)
      y_pred = self.predict_proba(support, task_dataset)
      task_scores[task].append(metric.compute_metric(
          task_dataset.y, y_pred, task_dataset.w))

    # Join information for all tasks.
    mean_task_scores = {}
    for task in test_tasks:
      mean_task_scores[task] = np.mean(np.array(task_scores[task]))
    return mean_task_scores
