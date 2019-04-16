import argparse
import sys

import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
from datasets import *
from metrics import recall_pairs, beam_occurrences
from nltk.translate.bleu_score import corpus_bleu
from tqdm import tqdm

from train import (
    MODEL_SHOW_ATTEND_TELL,
    MODEL_BOTTOM_UP_TOP_DOWN,
    MODEL_BOTTOM_UP_TOP_DOWN_RANKING,
)
from utils import (
    get_caption_without_special_tokens,
    IMAGENET_IMAGES_MEAN,
    IMAGENET_IMAGES_STD,
    IMAGES_FILENAME,
    BOTTOM_UP_FEATURES_FILENAME,
    get_splits,
)
from visualize_attention import visualize_attention

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
cudnn.benchmark = True  # improve performance if inputs to model are fixed size

METRIC_BLEU = "bleu"
METRIC_RECALL = "recall"
METRIC_BEAM_OCCURRENCES = "beam-occurrences"


def evaluate(
    data_folder,
    occurrences_data,
    karpathy_json,
    checkpoint,
    metrics,
    beam_size,
    visualize,
    print_beam,
):
    # Load model
    checkpoint = torch.load(checkpoint, map_location=device)

    model_name = checkpoint["model_name"]
    print("Model: {}".format(model_name))

    encoder = checkpoint["encoder"]
    if encoder:
        encoder = encoder.to(device)
        encoder.eval()

    decoder = checkpoint["decoder"]
    decoder = decoder.to(device)
    word_map = decoder.word_map
    decoder.eval()

    print("Decoder params: {}".format(decoder.params))

    _, _, test_images_split = get_splits(occurrences_data, karpathy_json)

    if model_name == MODEL_SHOW_ATTEND_TELL:
        # Normalization
        normalize = transforms.Normalize(
            mean=IMAGENET_IMAGES_MEAN, std=IMAGENET_IMAGES_STD
        )

        # DataLoader
        data_loader = torch.utils.data.DataLoader(
            CaptionTestDataset(
                data_folder,
                IMAGES_FILENAME,
                test_images_split,
                transforms.Compose([normalize]),
                features_scale_factor=1 / 255.0,
            ),
            batch_size=1,
            shuffle=True,
            num_workers=1,
            pin_memory=True,
        )
    elif (
        model_name == MODEL_BOTTOM_UP_TOP_DOWN
        or model_name == MODEL_BOTTOM_UP_TOP_DOWN_RANKING
    ):
        data_loader = torch.utils.data.DataLoader(
            CaptionTestDataset(
                data_folder, BOTTOM_UP_FEATURES_FILENAME, test_images_split
            ),
            batch_size=1,
            shuffle=True,
            num_workers=1,
            pin_memory=True,
        )
    else:
        raise RuntimeError("Unknown model name: {}".format(model_name))

    # Lists for target captions and generated captions for each image
    target_captions = []
    generated_captions = []
    generated_beams = []
    coco_ids = []

    for image_features, all_captions_for_image, _, coco_id in tqdm(
        data_loader, desc="Evaluate with beam size " + str(beam_size)
    ):

        # Target captions
        target_captions.append(
            [
                get_caption_without_special_tokens(caption, word_map)
                for caption in all_captions_for_image[0].tolist()
            ]
        )

        # Generate captions
        encoded_features = image_features.to(device)
        if encoder:
            encoded_features = encoder(encoded_features)

        store_beam = True if METRIC_BEAM_OCCURRENCES in metrics else False

        top_k_generated_captions, alphas, beam = decoder.beam_search(
            encoded_features,
            beam_size,
            store_alphas=visualize,
            store_beam=store_beam,
            print_beam=print_beam,
        )
        if visualize:
            print("Image COCO ID: {}".format(coco_id[0]))
            for caption, alpha in zip(top_k_generated_captions, alphas):
                visualize_attention(
                    image_features.squeeze(0), caption, alpha, word_map, smoothen=True
                )

        generated_captions.append(top_k_generated_captions)
        generated_beams.append(beam)

        coco_ids.append(coco_id[0])

        assert len(target_captions) == len(generated_captions)

    # Calculate metric scores
    for metric in metrics:
        metric_score = calculate_metric(
            metric,
            target_captions,
            generated_captions,
            generated_beams,
            word_map,
            occurrences_data,
            beam_size,
        )


def calculate_metric(
    metric_name,
    target_captions,
    generated_captions,
    generated_beams,
    word_map,
    occurrences_data,
    beam_size,
):
    if metric_name == METRIC_BLEU:
        generated_captions = [
            get_caption_without_special_tokens(top_k_captions[0], word_map)
            for top_k_captions in generated_captions
        ]
        bleu_1 = corpus_bleu(target_captions, generated_captions, weights=(1, 0, 0, 0))
        bleu_2 = corpus_bleu(
            target_captions, generated_captions, weights=(0.5, 0.5, 0, 0)
        )
        bleu_3 = corpus_bleu(
            target_captions, generated_captions, weights=(0.33, 0.33, 0.33, 0)
        )
        bleu_4 = corpus_bleu(
            target_captions, generated_captions, weights=(0.25, 0.25, 0.25, 0.25)
        )
        bleu_scores = [bleu_1, bleu_2, bleu_3, bleu_4]
        bleu_scores = [float("%.2f" % elem) for elem in bleu_scores]
        print("\nBLEU score @ beam size {} is {}".format(beam_size, bleu_scores))
    elif metric_name == METRIC_RECALL:
        recall_pairs(generated_captions, word_map, occurrences_data)
    elif metric_name == METRIC_BEAM_OCCURRENCES:
        beam_occurrences_score = beam_occurrences(
            generated_beams, beam_size, word_map, occurrences_data
        )
        print(
            "\nBeam occurrences score @ beam size {} is {}".format(
                beam_size, beam_occurrences_score
            )
        )


def check_args(args):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-folder",
        help="Folder where the preprocessed data is located",
        default=os.path.expanduser("../datasets/coco2014_preprocessed/"),
    )
    parser.add_argument(
        "--occurrences-data",
        nargs="+",
        help="Files containing occurrences statistics about adjective-noun or verb-noun pairs",
    )
    parser.add_argument(
        "--karpathy-json", help="File containing train/val/test split information"
    )
    parser.add_argument(
        "--checkpoint", help="Path to checkpoint of trained model", required=True
    )
    parser.add_argument(
        "--metrics",
        help="Evaluation metrics",
        nargs="+",
        default=[METRIC_BLEU],
        choices=[METRIC_BLEU, METRIC_RECALL, METRIC_BEAM_OCCURRENCES],
    )

    parser.add_argument(
        "--beam-size", help="Size of the decoding beam", type=int, default=1
    )
    parser.add_argument(
        "--visualize-attention",
        help="Visualize the attention for every sample",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--print-beam",
        help="Print the decoding beam for every sample",
        default=False,
        action="store_true",
    )

    parsed_args = parser.parse_args(args)
    print(parsed_args)
    return parsed_args


if __name__ == "__main__":
    parsed_args = check_args(sys.argv[1:])
    evaluate(
        data_folder=parsed_args.data_folder,
        occurrences_data=parsed_args.occurrences_data,
        karpathy_json=parsed_args.karpathy_json,
        checkpoint=parsed_args.checkpoint,
        metrics=parsed_args.metrics,
        beam_size=parsed_args.beam_size,
        visualize=parsed_args.visualize_attention,
        print_beam=parsed_args.print_beam,
    )
