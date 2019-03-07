import json
import os

import h5py
import torch

from scipy.misc import imread, imresize
import matplotlib.pyplot as plt
import numpy as np

TOKEN_UNKNOWN = "<unk>"
TOKEN_START = "<start>"
TOKEN_END = "<end>"
TOKEN_PADDING = "<pad>"

# Normalization for images (cf. https://pytorch-zh.readthedocs.io/en/latest/torchvision/models.html)
IMAGENET_IMAGES_MEAN = [0.485, 0.456, 0.406]
IMAGENET_IMAGES_STD = [0.229, 0.224, 0.225]


WORD_MAP_FILENAME = "word_map.json"
IMAGES_FILENAME = "images.hdf5"
CAPTIONS_FILENAME = "captions.json"
CAPTION_LENGTHS_FILENAME = "caption_lengths.json"

NOUNS = "nouns"
ADJECTIVES = "adjectives"

OCCURRENCE_DATA = "adjective_noun_occurrence_data"
PAIR_OCCURENCES = "pair_occurrences"
NOUN_OCCURRENCES = "noun_occurrences"
ADJECTIVE_OCCURRENCES = "adjective_occurrences"

RELATION_NOMINAL_SUBJECT = "nsubj"
RELATION_ADJECTIVAL_MODIFIER = "amod"
RELATION_CONJUNCT = "conj"


def contains_adjective_noun_pair(nlp_pipeline, caption, nouns, adjectives):
    noun_is_present = False
    adjective_is_present = False

    doc = nlp_pipeline(caption)
    sentence = doc.sentences[0]

    for token in sentence.tokens:
        if token.text in nouns:
            noun_is_present = True
        if token.text in adjectives:
            adjective_is_present = True

    dependencies = sentence.dependencies
    caption_adjectives = {
        d[2].text
        for d in dependencies
        if d[1] == RELATION_ADJECTIVAL_MODIFIER and d[0].text in nouns
    } | {
        d[0].text
        for d in dependencies
        if d[1] == RELATION_NOMINAL_SUBJECT and d[2].text in nouns
    }
    conjuncted_caption_adjectives = set()
    for adjective in caption_adjectives:
        conjuncted_caption_adjectives.update(
            {
                d[2].text
                for d in dependencies
                if d[1] == RELATION_CONJUNCT and d[0].text == adjective
            }
            | {
                d[2].text
                for d in dependencies
                if d[1] == RELATION_ADJECTIVAL_MODIFIER and d[0].text == adjective
            }
        )

    caption_adjectives |= conjuncted_caption_adjectives
    combination_is_present = bool(adjectives & caption_adjectives)

    return noun_is_present, adjective_is_present, combination_is_present


def read_image(path):
    img = imread(path)
    if len(img.shape) == 2:  # b/w image
        img = img[:, :, np.newaxis]
        img = np.concatenate([img, img, img], axis=2)
    img = imresize(img, (256, 256))
    img = img.transpose(2, 0, 1)
    assert img.shape == (3, 256, 256)
    assert np.max(img) <= 255
    return img


def get_splits_from_occurrences_data(
    data_folder, occurrences_data_file, val_set_size=0
):
    with open(occurrences_data_file, "r") as json_file:
        occurrences_data = json.load(json_file)

    test_images_split = [
        key
        for key, value in occurrences_data[OCCURRENCE_DATA].items()
        if value[PAIR_OCCURENCES] >= 1
    ]
    # test_images_split = [str(id) for id in test_set_image_coco_ids]

    h5py_file = h5py.File(os.path.join(data_folder, IMAGES_FILENAME), "r")
    all_coco_ids = list(h5py_file.keys())

    indices_without_test = list(set(all_coco_ids) - set(test_images_split))

    train_val_split = int((1 - val_set_size) * len(indices_without_test))
    train_images_split = indices_without_test[:train_val_split]
    val_images_split = indices_without_test[train_val_split:]

    return train_images_split, val_images_split, test_images_split


def show_img(img):
    plt.imshow(img.transpose(1, 2, 0))
    plt.show()


def decode_caption(encoded_caption, word_map):
    rev_word_map = {v: k for k, v in word_map.items()}
    return [rev_word_map[ind] for ind in encoded_caption]


def get_caption_without_special_tokens(caption, word_map):
    """Remove start, end and padding tokens from and encoded caption."""

    return [
        token
        for token in caption
        if token
        not in {word_map[TOKEN_START], word_map[TOKEN_END], word_map[TOKEN_PADDING]}
    ]


def clip_gradients(optimizer, grad_clip):
    """
    Clips gradients computed during backpropagation to avoid explosion of gradients.

    :param optimizer: optimizer with the gradients to be clipped
    :param grad_clip: clip value
    """
    for group in optimizer.param_groups:
        for param in group["params"]:
            if param.grad is not None:
                param.grad.data.clamp_(-grad_clip, grad_clip)


DEFAULT_CHECKPOINT_NAME = "checkpoint.pth.tar"
DEFAULT_BEST_CHECKPOINT_NAME = "best_" + DEFAULT_CHECKPOINT_NAME


def save_checkpoint(
    epoch,
    epochs_since_improvement,
    encoder,
    decoder,
    encoder_optimizer,
    decoder_optimizer,
    bleu4,
    is_best,
):
    """
    Save a model checkpoint.

    :param epoch: epoch number
    :param epochs_since_improvement: number of epochs since last improvement
    :param encoder: encoder model
    :param decoder: decoder model
    :param encoder_optimizer: optimizer to update the encoder's weights
    :param decoder_optimizer: optimizer to update the decoder's weights
    :param bleu4: validation set BLEU-4 score for this epoch
    :param is_best: True, if this is the best checkpoint so far (will save the model to a dedicated file)
    """
    state = {
        "epoch": epoch,
        "epochs_since_improvement": epochs_since_improvement,
        "bleu-4": bleu4,
        "encoder": encoder,
        "decoder": decoder,
        "encoder_optimizer": encoder_optimizer,
        "decoder_optimizer": decoder_optimizer,
    }
    filename = DEFAULT_CHECKPOINT_NAME
    torch.save(state, filename)

    # If this checkpoint is the best so far, store a copy so it doesn't get overwritten by a worse checkpoint
    if is_best:
        torch.save(state, DEFAULT_BEST_CHECKPOINT_NAME)


class AverageMeter(object):
    """Class to keep track of most recent, average, sum, and count of a metric."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, shrink_factor):
    """
    Shrink the learning rate by a specified factor.

    :param optimizer: optimizer whose learning rate should be shrunk.
    :param shrink_factor: factor to multiply learning rate with.
    """

    print("\nAdjusting learning rate.")
    for param_group in optimizer.param_groups:
        param_group["lr"] = param_group["lr"] * shrink_factor
    print("The new learning rate is {}\n".format(optimizer.param_groups[0]["lr"]))


def top_k_accuracy(scores, targets, k):
    """
    Compute the top-k accuracy from predicted and true labels.

    :param scores: predicted scores from the model
    :param targets: true labels
    :param k: k
    :return: top-k accuracy
    """

    batch_size = targets.size(0)
    _, ind = scores.topk(k, 1, True, True)
    correct = ind.eq(targets.view(-1, 1).expand_as(ind))
    correct_total = correct.view(-1).float().sum()
    return correct_total.item() * (100.0 / batch_size)
