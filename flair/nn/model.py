import itertools
import logging
import typing
import warnings
from abc import abstractmethod
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import torch.nn
from torch.nn.modules.loss import _Loss
from torch.utils.data.dataset import Dataset
from tqdm import tqdm

import flair
from flair import file_utils
from flair.data import DT, Dictionary, Label, Sentence
from flair.datasets import DataLoader, FlairDatapointDataset
from flair.training_utils import Result, store_embeddings

log = logging.getLogger("flair")


class Model(torch.nn.Module, typing.Generic[DT]):
    """Abstract base class for all downstream task models in Flair,
    such as SequenceTagger and TextClassifier.
    Every new type of model must implement these methods."""

    model_card: Optional[Dict[str, Any]] = None

    @property
    @abstractmethod
    def label_type(self):
        """Each model predicts labels of a certain type.
        TODO: can we find a better name for this?"""
        raise NotImplementedError

    @abstractmethod
    def forward_loss(self, data_points: Union[List[DT], DT]) -> Union[torch.Tensor, Tuple[torch.Tensor, int]]:
        """Performs a forward pass and returns a loss tensor for backpropagation.
        Implement this to enable training."""
        raise NotImplementedError

    @abstractmethod
    def evaluate(
        self,
        data_points: Union[List[DT], Dataset],
        gold_label_type: str,
        out_path: Union[str, Path] = None,
        embedding_storage_mode: str = "none",
        mini_batch_size: int = 32,
        num_workers: Optional[int] = 8,
        **kwargs,
    ) -> Result:
        """Evaluates the model. Returns a Result object containing evaluation
        results and a loss value. Implement this to enable evaluation.
        :param data_loader: DataLoader that iterates over dataset to be evaluated
        :param out_path: Optional output path to store predictions
        :param embedding_storage_mode: One of 'none', 'cpu' or 'gpu'. 'none' means all embeddings are deleted and freshly recomputed, 'cpu' means all embeddings are stored on CPU, or 'gpu' means all embeddings are stored on GPU  # noqa: E501
        :return: Returns a Tuple consisting of a Result object and a loss float value
        """
        raise NotImplementedError

    @abstractmethod
    def _get_state_dict(self):
        """Returns the state dictionary for this model.
        Implementing this enables the save() and save_checkpoint()
        functionality."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def _init_model_with_state_dict(state):
        """Initialize the model from a state dictionary.
        Implementing this enables the load() and load_checkpoint()
        functionality."""
        raise NotImplementedError

    @staticmethod
    def _fetch_model(model_name) -> str:
        return model_name

    def save(self, model_file: Union[str, Path], checkpoint: bool = False):
        """
        Saves the current model to the provided file.
        :param model_file: the model file
        """
        model_state = self._get_state_dict()

        # in Flair <0.9.1, optimizer and scheduler used to train model are not saved
        optimizer = scheduler = None

        # write out a "model card" if one is set
        if self.model_card is not None:

            # special handling for optimizer:
            # remember optimizer class and state dictionary
            if "training_parameters" in self.model_card:
                training_parameters = self.model_card["training_parameters"]

                if "optimizer" in training_parameters:
                    optimizer = training_parameters["optimizer"]
                    if checkpoint:
                        training_parameters["optimizer_state_dict"] = optimizer.state_dict()
                    training_parameters["optimizer"] = optimizer.__class__

                if "scheduler" in training_parameters:
                    scheduler = training_parameters["scheduler"]
                    if checkpoint:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            training_parameters["scheduler_state_dict"] = scheduler.state_dict()
                    training_parameters["scheduler"] = scheduler.__class__

            model_state["model_card"] = self.model_card

        # save model
        torch.save(model_state, str(model_file), pickle_protocol=4)

        # restore optimizer and scheduler to model card if set
        if self.model_card is not None:
            if optimizer:
                self.model_card["training_parameters"]["optimizer"] = optimizer
            if scheduler:
                self.model_card["training_parameters"]["scheduler"] = scheduler

    @classmethod
    def load(cls, model_path: Union[str, Path], ff_dim=2048, nhead=8, fine_tune=False,num_layers=1):
        """
        Loads the model from the given file.
        :param model_path: the model file
        :return: the loaded text classifier model
        """
        model_file = cls._fetch_model(str(model_path))

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            # load_big_file is a workaround byhttps://github.com/highway11git
            # to load models on some Mac/Windows setups
            # see https://github.com/zalandoresearch/flair/issues/351
            f = file_utils.load_big_file(str(model_file))
            state = torch.load(f, map_location="cpu")

        model = cls._init_model_with_state_dict(state,num_layers=num_layers, ff_dim=ff_dim, nhead=nhead,fine_tune=fine_tune)

        if "model_card" in state:
            model.model_card = state["model_card"]

        model.eval()
        model.to(flair.device)

        return model

    def print_model_card(self):
        if hasattr(self, "model_card"):
            param_out = "\n------------------------------------\n"
            param_out += "--------- Flair Model Card ---------\n"
            param_out += "------------------------------------\n"
            param_out += "- this Flair model was trained with:\n"
            param_out += f"-- Flair version {self.model_card['flair_version']}\n"
            param_out += f"-- PyTorch version {self.model_card['pytorch_version']}\n"
            if "transformers_version" in self.model_card:
                param_out += "-- Transformers version " f"{self.model_card['transformers_version']}\n"
            param_out += "------------------------------------\n"

            param_out += "------- Training Parameters: -------\n"
            param_out += "------------------------------------\n"
            training_params = "\n".join(
                f'-- {param} = {self.model_card["training_parameters"][param]}'
                for param in self.model_card["training_parameters"]
            )
            param_out += training_params + "\n"
            param_out += "------------------------------------\n"

            log.info(param_out)
        else:
            log.info(
                "This model has no model card (likely because it is not yet "
                "trained or was trained with Flair version < 0.9.1)"
            )


class Classifier(Model[DT], typing.Generic[DT]):
    """Abstract base class for all Flair models that do classification,
    both single- and multi-label. It inherits from flair.nn.Model and adds an
    unified evaluate() function so that all classification models use the same
    evaluation routines and compute the same numbers."""

    def evaluate(
        self,
        data_points: Union[List[DT], Dataset],
        gold_label_type: str,
        out_path: Union[str, Path] = None,
        embedding_storage_mode: str = "none",
        mini_batch_size: int = 32,
        num_workers: Optional[int] = 8,
        main_evaluation_metric: Tuple[str, str] = ("micro avg", "f1-score"),
        exclude_labels: List[str] = [],
        gold_label_dictionary: Optional[Dictionary] = None,
        **kwargs,
    ) -> Result:
        import numpy as np
        import sklearn

        # read Dataset into data loader, if list of sentences passed, make Dataset first
        if not isinstance(data_points, Dataset):
            data_points = FlairDatapointDataset(data_points)
        data_loader = DataLoader(data_points, batch_size=mini_batch_size, num_workers=num_workers)

        with torch.no_grad():

            # loss calculation
            eval_loss = torch.zeros(1, device=flair.device)
            average_over = 0

            # variables for printing
            lines: List[str] = []

            # variables for computing scores
            all_spans: List[str] = []
            all_true_values = {}
            all_predicted_values = {}

            sentence_id = 0
            for batch in data_loader:

                # remove any previously predicted labels
                for datapoint in batch:
                    datapoint.remove_labels("predicted")
                # predict for batch
                loss_and_count = self.predict(
                    batch,
                    embedding_storage_mode=embedding_storage_mode,
                    mini_batch_size=mini_batch_size,
                    label_name="predicted",
                    return_loss=True,
                )

                if isinstance(loss_and_count, tuple):
                    average_over += loss_and_count[1]
                    eval_loss += loss_and_count[0]
                else:
                    eval_loss += loss_and_count

                # get the gold labels
                for datapoint in batch:

                    for gold_label in datapoint.get_labels(gold_label_type):
                        representation = str(sentence_id) + ": " + gold_label.identifier

                        value = gold_label.value
                        if gold_label_dictionary and gold_label_dictionary.get_idx_for_item(value) == 0:
                            value = "<unk>"

                        if representation not in all_true_values:
                            all_true_values[representation] = [value]
                        else:
                            all_true_values[representation].append(value)

                        if representation not in all_spans:
                            all_spans.append(representation)

                    for predicted_span in datapoint.get_labels("predicted"):
                        representation = str(sentence_id) + ": " + predicted_span.identifier

                        # add to all_predicted_values
                        if representation not in all_predicted_values:
                            all_predicted_values[representation] = [predicted_span.value]
                        else:
                            all_predicted_values[representation].append(predicted_span.value)

                        if representation not in all_spans:
                            all_spans.append(representation)

                    sentence_id += 1

                store_embeddings(batch, embedding_storage_mode)

                # make printout lines
                if out_path:
                    lines.extend(self._print_predictions(batch, gold_label_type))

            # write all_predicted_values to out_file if set
            if out_path:
                with open(Path(out_path), "w", encoding="utf-8") as outfile:
                    outfile.write("".join(lines))

            # make the evaluation dictionary
            evaluation_label_dictionary = Dictionary(add_unk=False)
            evaluation_label_dictionary.add_item("O")
            for true_values in all_true_values.values():
                for label in true_values:
                    evaluation_label_dictionary.add_item(label)
            for predicted_values in all_predicted_values.values():
                for label in predicted_values:
                    evaluation_label_dictionary.add_item(label)

            # finally, compute numbers
            y_true = []
            y_pred = []

            for span in all_spans:

                true_values = all_true_values[span] if span in all_true_values else ["O"]
                predicted_values = all_predicted_values[span] if span in all_predicted_values else ["O"]

                y_true_instance = np.zeros(len(evaluation_label_dictionary), dtype=int)
                for true_value in true_values:
                    y_true_instance[evaluation_label_dictionary.get_idx_for_item(true_value)] = 1
                y_true.append(y_true_instance.tolist())

                y_pred_instance = np.zeros(len(evaluation_label_dictionary), dtype=int)
                for predicted_value in predicted_values:
                    y_pred_instance[evaluation_label_dictionary.get_idx_for_item(predicted_value)] = 1
                y_pred.append(y_pred_instance.tolist())

        # now, calculate evaluation numbers
        target_names = []
        labels = []

        counter = Counter(itertools.chain.from_iterable(all_true_values.values()))
        counter.update(list(itertools.chain.from_iterable(all_predicted_values.values())))

        for label_name, count in counter.most_common():
            if label_name == "O":
                continue
            if label_name in exclude_labels:
                continue
            target_names.append(label_name)
            labels.append(evaluation_label_dictionary.get_idx_for_item(label_name))

        # there is at least one gold label or one prediction (default)
        if len(all_true_values) + len(all_predicted_values) > 1:
            classification_report = sklearn.metrics.classification_report(
                y_true,
                y_pred,
                digits=4,
                target_names=target_names,
                zero_division=0,
                labels=labels,
            )

            classification_report_dict = sklearn.metrics.classification_report(
                y_true,
                y_pred,
                target_names=target_names,
                zero_division=0,
                output_dict=True,
                labels=labels,
            )

            accuracy_score = round(sklearn.metrics.accuracy_score(y_true, y_pred), 4)

            precision_score = round(classification_report_dict["micro avg"]["precision"], 4)
            recall_score = round(classification_report_dict["micro avg"]["recall"], 4)
            micro_f_score = round(classification_report_dict["micro avg"]["f1-score"], 4)
            macro_f_score = round(classification_report_dict["macro avg"]["f1-score"], 4)

            main_score = classification_report_dict[main_evaluation_metric[0]][main_evaluation_metric[1]]

        else:
            # issue error and default all evaluation numbers to 0.
            log.error(
                "ACHTUNG! No gold labels and no all_predicted_values found! "
                "Could be an error in your corpus or how you "
                "initialize the trainer!"
            )
            accuracy_score = precision_score = recall_score = micro_f_score = macro_f_score = main_score = 0.0
            classification_report = ""
            classification_report_dict = {}

        detailed_result = (
            "\nResults:"
            f"\n- F-score (micro) {micro_f_score}"
            f"\n- F-score (macro) {macro_f_score}"
            f"\n- Accuracy {accuracy_score}"
            "\n\nBy class:\n" + classification_report
        )

        # line for log file
        log_header = "PRECISION\tRECALL\tF1\tACCURACY"
        log_line = f"{precision_score}\t" f"{recall_score}\t" f"{micro_f_score}\t" f"{accuracy_score}"

        if average_over > 0:
            eval_loss /= average_over

        result = Result(
            main_score=main_score,
            log_line=log_line,
            log_header=log_header,
            detailed_results=detailed_result,
            classification_report=classification_report_dict,
            loss=eval_loss.item(),
        )

        return result

    def predict(
        self,
        sentences: Union[List[DT], DT],
        mini_batch_size: int = 32,
        return_probabilities_for_all_classes: bool = False,
        verbose: bool = False,
        label_name: Optional[str] = None,
        return_loss=False,
        embedding_storage_mode="none",
    ):
        """
        Predicts the class labels for the given sentences. The labels are directly added to the sentences.  # noqa: E501
        :param sentences: list of sentences
        :param mini_batch_size: mini batch size to use
        :param return_probabilities_for_all_classes : return probabilities for all classes instead of only best predicted  # noqa: E501
        :param verbose: set to True to display a progress bar
        :param return_loss: set to True to return loss
        :param label_name: set this to change the name of the label type that is predicted  # noqa: E501
        :param embedding_storage_mode: default is 'none' which is always best. Only set to 'cpu' or 'gpu' if you wish to not only predict, but also keep the generated embeddings in CPU or GPU memory respectively. 'gpu' to store embeddings in GPU memory.  # noqa: E501
        """
        raise NotImplementedError

    def _print_predictions(self, batch, gold_label_type):
        lines = []
        for datapoint in batch:
            # check if there is a label mismatch
            g = [label.identifier + label.value for label in datapoint.get_labels(gold_label_type)]
            p = [label.identifier + label.value for label in datapoint.get_labels("predicted")]
            g.sort()
            p.sort()
            correct_string = " -> MISMATCH!\n" if g != p else ""
            # print info
            eval_line = (
                f"{datapoint.to_original_text()}\n"
                f" - Gold: {datapoint.get_labels(gold_label_type)}\n"
                f" - Pred: {datapoint.get_labels('predicted')}\n{correct_string}\n"
            )
            lines.append(eval_line)
        return lines


class DefaultClassifier(Classifier[DT], typing.Generic[DT]):
    """Default base class for all Flair models that do classification, both
    single- and multi-label. It inherits from flair.nn.Classifier and thus from
    flair.nn.Model. All features shared by all classifiers are implemented here,
    including the loss calculation and the predict() method. Currently, the
    TextClassifier, RelationExtractor, TextPairClassifier and
    SimpleSequenceTagger implement this class. You only need to implement the
    forward_pass() method to implement this base class.
    """

    def forward_pass(
        self,
        sentences: Union[List[DT], DT],
        return_label_candidates: bool = False,
    ) -> Union[Tuple[torch.Tensor, List[List[str]]], Tuple[torch.Tensor, List[List[str]], List[DT], List[Label]]]:
        """This method does a forward pass through the model given a list of data
        points as input.
        Returns the tuple (scores, labels) if return_label_candidates = False,
        where scores are a tensor of logits produced by the decoder and labels
        are the string labels for each data point. Returns the tuple (scores,
        labels, data_points, candidate_labels) if return_label_candidates = True,
        where data_points are the data points to which labels are added (commonly
        either Sentence or Token objects) and candidate_labels are empty Label
        objects for each prediction (depending on the task Label, SpanLabel or
        RelationLabel)."""
        raise NotImplementedError

    def __init__(
        self,
        label_dictionary: Dictionary,
        multi_label: bool = False,
        multi_label_threshold: float = 0.5,
        loss_weights: Dict[str, float] = None,
    ):

        super().__init__()

        # initialize the label dictionary
        self.label_dictionary: Dictionary = label_dictionary

        # set up multi-label logic
        self.multi_label = multi_label
        self.multi_label_threshold = multi_label_threshold

        # loss weights and loss function
        self.weight_dict = loss_weights
        # Initialize the weight tensor
        if loss_weights is not None:
            n_classes = len(self.label_dictionary)
            weight_list = [1.0 for i in range(n_classes)]
            for i, tag in enumerate(self.label_dictionary.get_items()):
                if tag in loss_weights.keys():
                    weight_list[i] = loss_weights[tag]
            self.loss_weights: Optional[torch.Tensor] = torch.FloatTensor(weight_list).to(flair.device)
        else:
            self.loss_weights = None

        if self.multi_label:
            self.loss_function: _Loss = torch.nn.BCEWithLogitsLoss(weight=self.loss_weights)
        else:
            self.loss_function = torch.nn.CrossEntropyLoss(weight=self.loss_weights)

    @property
    def multi_label_threshold(self):
        return self._multi_label_threshold

    @multi_label_threshold.setter
    def multi_label_threshold(self, x):  # setter method
        if type(x) is dict:
            if "default" in x:
                self._multi_label_threshold = x
            else:
                raise Exception('multi_label_threshold dict should have a "default" key')
        else:
            self._multi_label_threshold = {"default": x}

    def forward_loss(self, sentences: Union[List[DT], DT]) -> Tuple[torch.Tensor, int]:
        scores, labels = self.forward_pass(sentences)  # type: ignore
        return self._calculate_loss(scores, labels)

    def _calculate_loss(self, scores, labels) -> Tuple[torch.Tensor, int]:

        if not any(labels):
            return torch.tensor(0.0, requires_grad=True, device=flair.device), 1

        if self.multi_label:
            labels = torch.tensor(
                [
                    [1 if label in all_labels_for_point else 0 for label in self.label_dictionary.get_items()]
                    for all_labels_for_point in labels
                ],
                dtype=torch.float,
                device=flair.device,
            )

        else:
            labels = torch.tensor(
                [
                    self.label_dictionary.get_idx_for_item(label[0])
                    if len(label) > 0
                    else self.label_dictionary.get_idx_for_item("O")
                    for label in labels
                ],
                dtype=torch.long,
                device=flair.device,
            )

        return self.loss_function(scores, labels), len(labels)

    def _sort_data(self, data_points: List[DT]) -> List[DT]:

        if len(data_points) == 0:
            return []

        if not isinstance(data_points[0], Sentence):
            return data_points

        # filter empty sentences
        sentences = [sentence for sentence in typing.cast(List[Sentence], data_points) if len(sentence) > 0]

        # reverse sort all sequences by their length
        reordered_sentences = sorted(sentences, key=lambda s: len(s), reverse=True)

        return typing.cast(List[DT], reordered_sentences)

    def predict(
        self,
        sentences: Union[List[DT], DT],
        mini_batch_size: int = 32,
        return_probabilities_for_all_classes: bool = False,
        verbose: bool = False,
        label_name: Optional[str] = None,
        return_loss=False,
        embedding_storage_mode="none",
    ):
        """
        Predicts the class labels for the given sentences. The labels are directly added to the sentences.  # noqa: E501
        :param sentences: list of sentences
        :param mini_batch_size: mini batch size to use
        :param return_probabilities_for_all_classes : return probabilities for all classes instead of only best predicted  # noqa: E501
        :param verbose: set to True to display a progress bar
        :param return_loss: set to True to return loss
        :param label_name: set this to change the name of the label type that is predicted  # noqa: E501
        :param embedding_storage_mode: default is 'none' which is always best. Only set to 'cpu' or 'gpu' if you wish to not only predict, but also keep the generated embeddings in CPU or GPU memory respectively.  # noqa: E501
        'gpu' to store embeddings in GPU memory.
        """
        if label_name is None:
            label_name = self.label_type if self.label_type is not None else "label"

        with torch.no_grad():
            if not sentences:
                return sentences

            if not isinstance(sentences, list):
                sentences = [sentences]

            reordered_sentences = self._sort_data(sentences)

            if len(reordered_sentences) == 0:
                return sentences

            dataloader = DataLoader(
                dataset=FlairDatapointDataset(reordered_sentences),
                batch_size=mini_batch_size,
            )
            # progress bar for verbosity
            if verbose:
                progress_bar = tqdm(dataloader)
                progress_bar.set_description("Batch inference")
                dataloader = progress_bar

            overall_loss = 0
            label_count = 0
            for batch in dataloader:
                # stop if all sentences are empty
                if not batch:
                    continue

                scores, gold_labels, data_points, label_candidates = self.forward_pass(
                    batch, return_label_candidates=True  # type: ignore
                )
                # remove previously predicted labels of this type
                for sentence in data_points:
                    sentence.remove_labels(label_name)

                if return_loss:
                    overall_loss += self._calculate_loss(scores, gold_labels)[0]
                    label_count += len(label_candidates)

                # if anything could possibly be predicted
                if len(label_candidates) > 0:
                    if self.multi_label:
                        sigmoided = torch.sigmoid(scores)  # size: (n_sentences, n_classes)
                        n_labels = sigmoided.size(1)
                        for s_idx, (data_point, label_candidate) in enumerate(zip(data_points, label_candidates)):
                            for l_idx in range(n_labels):
                                label_value = self.label_dictionary.get_item_for_index(l_idx)
                                if label_value == "O":
                                    continue
                                label_threshold = self._get_label_threshold(label_value)
                                label_score = sigmoided[s_idx, l_idx].item()
                                if label_score > label_threshold or return_probabilities_for_all_classes:
                                    label = label_candidate.spawn(value=label_value, score=label_score)
                                    data_point.add_complex_label(label_name, label)
                    else:
                        softmax = torch.nn.functional.softmax(scores, dim=-1)

                        if return_probabilities_for_all_classes:
                            n_labels = softmax.size(1)
                            for s_idx, (data_point, label_candidate) in enumerate(zip(data_points, label_candidates)):
                                for l_idx in range(n_labels):
                                    label_value = self.label_dictionary.get_item_for_index(l_idx)
                                    if label_value == "O":
                                        continue
                                    label_score = softmax[s_idx, l_idx].item()
                                    label = label_candidate.spawn(value=label_value, score=label_score)
                                    data_point.add_complex_label(label_name, label)
                        else:
                            conf, idx = torch.max(softmax, dim=-1)
                            for data_point, label_candidate, c, i in zip(data_points, label_candidates, conf, idx):
                                label_value = self.label_dictionary.get_item_for_index(i.item())
                                if label_value == "O":
                                    continue
                                label = label_candidate.spawn(value=label_value, score=c.item())
                                data_point.add_complex_label(label_name, label)

                store_embeddings(batch, storage_mode=embedding_storage_mode)

            if return_loss:
                return overall_loss, label_count

    def _get_label_threshold(self, label_value):
        label_threshold = self.multi_label_threshold["default"]
        if label_value in self.multi_label_threshold:
            label_threshold = self.multi_label_threshold[label_value]

        return label_threshold

    def __str__(self):
        return (
            super(flair.nn.Model, self).__str__().rstrip(")")
            + f"  (weights): {self.weight_dict}\n"
            + f"  (weight_tensor) {self.loss_weights}\n)"
        )
