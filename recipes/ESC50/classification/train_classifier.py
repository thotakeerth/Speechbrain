#!/usr/bin/python3
"""Recipe to train a classifier on ESC50 data
We employ an encoder followed by a sound classifier.

To run this recipe, use the following command:
> python train_classifier.py hparams/cnn14.yaml --data_folder yourpath/ESC-50-master

Authors
    * Cem Subakan 2022, 2023
    * Francesco Paissan 2022, 2023

Based on the Urban8k recipe by
    * David Whipps 2021
    * Ala Eddine Limame 2021
"""
import os
import sys
import torch
import torchaudio
import speechbrain as sb
from hyperpyyaml import load_hyperpyyaml
from speechbrain.utils.distributed import run_on_main
from speechbrain.processing.speech_augmentation import AddNoise
from esc50_prepare import prepare_esc50
from wham_prepare import WHAMDataset, combine_batches
from urbansound8k_prepare import prepare_urban_sound_8k
from sklearn.metrics import confusion_matrix
import numpy as np
from confusion_matrix_fig import create_cm_fig
import torch.nn.functional as F
import torchvision


class ESC50Brain(sb.core.Brain):
    """Class for classifier training"
    """

    def compute_forward(self, batch, stage):
        """Computation pipeline based on a encoder + sound classifier.
        Data augmentation and environmental corruption are applied to the
        input sound.
        """
        batch = batch.to(self.device)
        wavs, lens = batch.sig

        # augment batch with WHAM!
        if hasattr(self.hparams, 'add_wham_noise'):
            if self.hparams.add_wham_noise:
                wavs = combine_batches(wavs, iter(self.hparams.wham_dataset))

        if stage == sb.Stage.TRAIN and hasattr(self.hparams, "augment"):
            assert not hparams["roar"], "You should not augment when running ROAR!"
            wavs, lens = self.hparams.augment(
                wavs,
                lengths=lens
            )

        X_stft = self.modules.compute_stft(wavs)
        X_stft_power = sb.processing.features.spectral_magnitude(
            X_stft, power=self.hparams.spec_mag_power
        )

        net_input = torch.log1p(X_stft_power)
        mel = self.hparams.compute_fbank(X_stft_power)
        mel = torch.log1p(mel)

        if stage == sb.Stage.TRAIN and self.hparams.spec_augment is not None:
            assert self.hparams.roar, "You sure you want to run ROAR with spec augment?"
            net_input = self.hparams.spec_augment(net_input, lens)

        if hasattr(self.hparams, "roar"):
            if self.hparams.roar:
                # Embeddings + sound classifier
                temp = self.hparams.pretrained_c(mel)
                if isinstance(temp, tuple):
                    embeddings, f_I = temp
                else:
                    embeddings, f_I = temp, temp
        
                if embeddings.ndim == 4:
                    embeddings = embeddings.mean((-1, -2))
        
                int_ = self.hparams.psi_model(f_I)
                int_ = torch.sigmoid(int_)
                thr = torch.quantile(int_.view(int_.shape[0], -1), torch.Tensor([self.hparams.roar_th]).to(int_.device), dim=-1).squeeze().view(-1, 1, 1, 1)
                torchvision.utils.save_image(mel[0:1].unsqueeze(1), "inp.png")
                torchvision.utils.save_image(int_, "mask.png")
                int_ = (int_ >= thr).float()
                torchvision.utils.save_image(int_, "mask_th.png")

                breakpoint()

                # # avg
                # # C = images.mean((-1, -2, -3)).view(-1, 1, 1, 1)
                # C = torch.Tensor([0.485, 0.456, 0.406]) # channel-wise ImageNet mean
                # C = C.view(-1, 3, 1, 1).to(int_.device)
                # # remove useful stuff
                # net_input = int_ * C
                # # replace lower ranked portions of the image
                # net_input += (1 - int_) * net_input


        net_input = torch.expm1(net_input)
        net_input = self.hparams.compute_fbank(X_stft_power)
        net_input = torch.log1p(mel)
        # Embeddings + sound classifier
        temp = self.modules.embedding_model(net_input)
        if isinstance(temp, tuple):
            embeddings, f_I = temp
        else:
            embeddings, f_I = temp, temp

        if embeddings.ndim == 4:
            embeddings = embeddings.mean((-1, -2))

        outputs = self.modules.classifier(embeddings)

        if outputs.ndim == 2:
            outputs = outputs.unsqueeze(1)

        # print(outputs.squeeze(1).softmax(1)[0:1])
        return outputs, lens

    def compute_objectives(self, predictions, batch, stage):
        """Computes the loss using class-id as label.
        """
        predictions, lens = predictions
        uttid = batch.id
        classid, _ = batch.class_string_encoded

        # Target augmentation
        N_augments = int(predictions.shape[0] / classid.shape[0])
        classid = torch.cat(N_augments * [classid], dim=0)

        # loss = self.hparams.compute_cost(predictions.squeeze(1), classid, lens)
        target = F.one_hot(classid.squeeze(), num_classes=self.hparams.out_n_neurons)
        loss = -(F.log_softmax(predictions.squeeze(), 1) * target).sum(1).mean()

        if stage != sb.Stage.TEST:
            if hasattr(self.hparams.lr_annealing, "on_batch_end"):
                self.hparams.lr_annealing.on_batch_end(self.optimizer)

        # Append this batch of losses to the loss metric for easy
        self.loss_metric.append(
            uttid, predictions, classid, lens, reduction="batch"
        )

        # Confusion matrices
        if stage != sb.Stage.TRAIN:
            y_true = classid.cpu().detach().numpy().squeeze(-1)
            y_pred = predictions.cpu().detach().numpy().argmax(-1).squeeze(-1)

        if stage == sb.Stage.VALID:
            confusion_matix = confusion_matrix(
                y_true,
                y_pred,
                labels=sorted(self.hparams.label_encoder.ind2lab.keys()),
            )
            self.valid_confusion_matrix += confusion_matix
        if stage == sb.Stage.TEST:
            confusion_matix = confusion_matrix(
                y_true,
                y_pred,
                labels=sorted(self.hparams.label_encoder.ind2lab.keys()),
            )
            self.test_confusion_matrix += confusion_matix

        # Compute Accuracy using MetricStats
        self.acc_metric.append(
            uttid, predict=predictions, target=classid, lengths=lens
        )

        if stage != sb.Stage.TRAIN:
            self.error_metrics.append(uttid, predictions, classid, lens)

        return loss

    def on_stage_start(self, stage, epoch=None):
        """Gets called at the beginning of each epoch.

        Arguments
        ---------
        stage : sb.Stage
            One of sb.Stage.TRAIN, sb.Stage.VALID, or sb.Stage.TEST.
        epoch : int
            The currently-starting epoch. This is passed
            `None` during the test stage.
        """
        # Set up statistics trackers for this stage
        self.loss_metric = sb.utils.metric_stats.MetricStats(
            metric=sb.nnet.losses.nll_loss
        )

        # Compute Accuracy using MetricStats
        # Define function taking (prediction, target, length) for eval
        def accuracy_value(predict, target, lengths):
            """Computes Accuracy"""
            nbr_correct, nbr_total = sb.utils.Accuracy.Accuracy(
                predict, target, lengths
            )
            acc = torch.tensor([nbr_correct / nbr_total])
            return acc

        self.acc_metric = sb.utils.metric_stats.MetricStats(
            metric=accuracy_value, n_jobs=1
        )

        # Confusion matrices
        if stage == sb.Stage.VALID:
            self.valid_confusion_matrix = np.zeros(
                shape=(self.hparams.out_n_neurons, self.hparams.out_n_neurons),
                dtype=int,
            )
        if stage == sb.Stage.TEST:
            self.test_confusion_matrix = np.zeros(
                shape=(self.hparams.out_n_neurons, self.hparams.out_n_neurons),
                dtype=int,
            )

        # Set up evaluation-only statistics trackers
        if stage != sb.Stage.TRAIN:
            self.error_metrics = self.hparams.error_stats()

    def on_stage_end(self, stage, stage_loss, epoch=None):
        """Gets called at the end of an epoch.

        Arguments
        ---------
        stage : sb.Stage
            One of sb.Stage.TRAIN, sb.Stage.VALID, sb.Stage.TEST
        stage_loss : float
            The average loss for all of the data processed in this stage.
        epoch : int
            The currently-starting epoch. This is passed
            `None` during the test stage.
        """
        # Compute/store important stats
        if stage == sb.Stage.TRAIN:
            self.train_loss = stage_loss
            self.train_stats = {
                "loss": self.train_loss,
                "acc": self.acc_metric.summarize("average"),
            }
        # Summarize Valid statistics from the stage for record-keeping.
        elif stage == sb.Stage.VALID:
            valid_stats = {
                "loss": stage_loss,
                "acc": self.acc_metric.summarize(
                    "average"
                ),  # "acc": self.valid_acc_metric.summarize(),
                "error": self.error_metrics.summarize("average"),
            }
        # Summarize Test statistics from the stage for record-keeping.
        else:
            test_stats = {
                "loss": stage_loss,
                "acc": self.acc_metric.summarize("average"),
                "error": self.error_metrics.summarize("average"),
            }

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:
            old_lr, new_lr = self.hparams.lr_annealing(epoch)
            sb.nnet.schedulers.update_learning_rate(self.optimizer, new_lr)

            # Tensorboard logging
            if self.hparams.use_tensorboard:
                self.hparams.tensorboard_train_logger.log_stats(
                    stats_meta={"Epoch": epoch},
                    train_stats=self.train_stats,
                    valid_stats=valid_stats,
                )
                # Log confusion matrix fig to tensorboard
                cm_fig = create_cm_fig(
                    self.valid_confusion_matrix,
                    display_labels=list(
                        self.hparams.label_encoder.ind2lab.values()
                    ),
                )
                self.hparams.tensorboard_train_logger.writer.add_figure(
                    "Validation Confusion Matrix", cm_fig, epoch
                )

            # Per class accuracy from Validation confusion matrix
            per_class_acc_arr = np.diag(self.valid_confusion_matrix) / np.sum(
                self.valid_confusion_matrix, axis=1
            )
            per_class_acc_arr_str = "\n" + "\n".join(
                "{:}: {:.3f}".format(
                    self.hparams.label_encoder.decode_ndim(class_id), class_acc
                )
                for class_id, class_acc in enumerate(per_class_acc_arr)
            )

            # The train_logger writes a summary to stdout and to the logfile.
            self.hparams.train_logger.log_stats(
                stats_meta={"epoch": epoch, "lr": old_lr},
                train_stats=self.train_stats,
                valid_stats=valid_stats,
            )
            # Save the current checkpoint and delete previous checkpoints,
            self.checkpointer.save_and_keep_only(
                meta=valid_stats, min_keys=["error"]
            )

        # We also write statistics about test data to stdout and to the logfile.
        if stage == sb.Stage.TEST:
            # Per class accuracy from Test confusion matrix
            per_class_acc_arr = np.diag(self.test_confusion_matrix) / np.sum(
                self.test_confusion_matrix, axis=1
            )
            per_class_acc_arr_str = "\n" + "\n".join(
                "{:}: {:.3f}".format(class_id, class_acc)
                for class_id, class_acc in enumerate(per_class_acc_arr)
            )

            self.hparams.train_logger.log_stats(
                {
                    "Epoch loaded": self.hparams.epoch_counter.current,
                    "\n Per Class Accuracy": per_class_acc_arr_str,
                    "\n Confusion Matrix": "\n{:}\n".format(
                        self.test_confusion_matrix
                    ),
                },
                test_stats=test_stats,
            )


def dataio_prep_esc50(hparams):
    "Creates the datasets and their data processing pipelines."

    data_audio_folder = hparams["audio_data_folder"]
    config_sample_rate = hparams["sample_rate"]
    label_encoder = sb.dataio.encoder.CategoricalEncoder()
    hparams["resampler"] = torchaudio.transforms.Resample(
        new_freq=config_sample_rate
    )

    # 2. Define audio pipeline:
    @sb.utils.data_pipeline.takes("wav")
    @sb.utils.data_pipeline.provides("sig")
    def audio_pipeline(wav):
        """Load the signal, and pass it and its length to the corruption class.
        This is done on the CPU in the `collate_fn`."""

        wave_file = data_audio_folder + "/{:}".format(wav)

        sig, read_sr = torchaudio.load(wave_file)

        # If multi-channels, downmix it to a mono channel
        sig = torch.squeeze(sig)
        if len(sig.shape) > 1:
            sig = torch.mean(sig, dim=0)

        # Convert sample rate to required config_sample_rate
        if read_sr != config_sample_rate:
            # Re-initialize sampler if source file sample rate changed compared to last file
            if read_sr != hparams["resampler"].orig_freq:
                hparams["resampler"] = torchaudio.transforms.Resample(
                    orig_freq=read_sr, new_freq=config_sample_rate
                )
            # Resample audio
            sig = hparams["resampler"].forward(sig)

        sig = sig.float()
        sig = sig / sig.max()
        return sig

    # 3. Define label pipeline:
    @sb.utils.data_pipeline.takes("class_string")
    @sb.utils.data_pipeline.provides("class_string", "class_string_encoded")
    def label_pipeline(class_string):
        yield class_string
        class_string_encoded = label_encoder.encode_label_torch(class_string)
        yield class_string_encoded

    # Define datasets. We also connect the dataset with the data processing
    # functions defined above.
    datasets = {}
    data_info = {
        "train": hparams["train_annotation"],
        "valid": hparams["valid_annotation"],
        "test": hparams["test_annotation"],
    }
    for dataset in data_info:
        datasets[dataset] = sb.dataio.dataset.DynamicItemDataset.from_json(
            json_path=data_info[dataset],
            replacements={"data_root": hparams["data_folder"]},
            dynamic_items=[audio_pipeline, label_pipeline],
            output_keys=["id", "sig", "class_string_encoded"],
        )

    # Load or compute the label encoder (with multi-GPU DDP support)
    # Please, take a look into the lab_enc_file to see the label to index
    # mappinng.
    lab_enc_file = os.path.join(hparams["save_folder"], "label_encoder.txt")
    label_encoder.load_or_create(
        path=lab_enc_file,
        from_didatasets=[datasets["train"]],
        output_key="class_string",
    )

    return datasets, label_encoder


def dataio_prep_us8k(hparams):
    "Creates the datasets and their data processing pipelines."

    data_audio_folder = hparams["audio_data_folder"]
    config_sample_rate = hparams["sample_rate"]
    label_encoder = sb.dataio.encoder.CategoricalEncoder()
    # TODO  use SB implementation but need to make sure it give the same results as PyTorch
    # resampler = sb.processing.speech_augmentation.Resample(orig_freq=latest_file_sr, new_freq=config_sample_rate)
    hparams["resampler"] = torchaudio.transforms.Resample(
        new_freq=config_sample_rate
    )

    # 2. Define audio pipeline:
    @sb.utils.data_pipeline.takes("wav", "fold")
    @sb.utils.data_pipeline.provides("sig")
    def audio_pipeline(wav, fold):
        """Load the signal, and pass it and its length to the corruption class.
        This is done on the CPU in the `collate_fn`."""

        wave_file = data_audio_folder + "/fold{:}/{:}".format(fold, wav)

        sig, read_sr = torchaudio.load(wave_file)

        # If multi-channels, downmix it to a mono channel
        sig = torch.squeeze(sig)
        if len(sig.shape) > 1:
            sig = torch.mean(sig, dim=0)

        # Convert sample rate to required config_sample_rate
        if read_sr != config_sample_rate:
            # Re-initialize sampler if source file sample rate changed compared to last file
            if read_sr != hparams["resampler"].orig_freq:
                hparams["resampler"] = torchaudio.transforms.Resample(
                    orig_freq=read_sr, new_freq=config_sample_rate
                )
            # Resample audio
            sig = hparams["resampler"].forward(sig)

            # pad to 4 seconds
            length = hparams["signal_length_s"] * hparams["sample_rate"]
            p = length - sig.shape[-1]
            sig = F.pad(sig, (0, p))

        return sig

    # 3. Define label pipeline:
    @sb.utils.data_pipeline.takes("class_string")
    @sb.utils.data_pipeline.provides("class_string", "class_string_encoded")
    def label_pipeline(class_string):
        yield class_string
        class_string_encoded = label_encoder.encode_label_torch(class_string)
        yield class_string_encoded

    # Define datasets. We also connect the dataset with the data processing
    # functions defined above.
    datasets = {}
    data_info = {
        "train": hparams["train_annotation"],
        "valid": hparams["valid_annotation"],
        "test": hparams["test_annotation"],
    }
    for dataset in data_info:
        datasets[dataset] = sb.dataio.dataset.DynamicItemDataset.from_json(
            json_path=data_info[dataset],
            replacements={"data_root": hparams["data_folder"]},
            dynamic_items=[audio_pipeline, label_pipeline],
            output_keys=["id", "sig", "class_string_encoded"],
        )

    # Load or compute the label encoder (with multi-GPU DDP support)
    # Please, take a look into the lab_enc_file to see the label to index
    # mappinng.
    lab_enc_file = os.path.join(hparams["save_folder"], "label_encoder.txt")
    label_encoder.load_or_create(
        path=lab_enc_file,
        from_didatasets=[datasets["train"]],
        output_key="class_string",
    )

    return datasets, label_encoder


if __name__ == "__main__":

    # This flag enables the inbuilt cudnn auto-tuner
    torch.backends.cudnn.benchmark = True

    # CLI:
    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])

    # Initialize ddp (useful only for multi-GPU DDP training)
    sb.utils.distributed.ddp_init_group(run_opts)

    # Load hyperparameters file with command-line overrides
    with open(hparams_file) as fin:
        hparams = load_hyperpyyaml(fin, overrides)

    # Create experiment directory
    sb.create_experiment_directory(
        experiment_directory=hparams["output_folder"],
        hyperparams_to_save=hparams_file,
        overrides=overrides,
    )

    # Tensorboard logging
    if hparams["use_tensorboard"]:
        from speechbrain.utils.train_logger import TensorboardLogger

        hparams["tensorboard_train_logger"] = TensorboardLogger(
            hparams["tensorboard_logs_folder"]
        )

    if hparams["dataset"] == "esc50":
        run_on_main(
            prepare_esc50,
            kwargs={
                "data_folder": hparams["data_folder"],
                "audio_data_folder": hparams["audio_data_folder"],
                "save_json_train": hparams["train_annotation"],
                "save_json_valid": hparams["valid_annotation"],
                "save_json_test": hparams["test_annotation"],
                "train_fold_nums": hparams["train_fold_nums"],
                "valid_fold_nums": hparams["valid_fold_nums"],
                "test_fold_nums": hparams["test_fold_nums"],
                "skip_manifest_creation": hparams["skip_manifest_creation"],
            },
        )

        # Dataset IO prep: creating Dataset objects and proper encodings for phones
        datasets, label_encoder = dataio_prep_esc50(hparams)
        hparams["label_encoder"] = label_encoder

    elif hparams["dataset"] == "us8k":
        run_on_main(
            prepare_urban_sound_8k,
            kwargs={
                "data_folder": hparams["data_folder"],
                "audio_data_folder": hparams["audio_data_folder"],
                "save_json_train": hparams["train_annotation"],
                "save_json_valid": hparams["valid_annotation"],
                "save_json_test": hparams["test_annotation"],
                "train_fold_nums": hparams["train_fold_nums"],
                "valid_fold_nums": hparams["valid_fold_nums"],
                "test_fold_nums": hparams["test_fold_nums"],
                "skip_manifest_creation": hparams["skip_manifest_creation"],
            },
        ) 

        # Dataset IO prep: creating Dataset objects and proper encodings for phones
        datasets, label_encoder = dataio_prep_us8k(hparams)
        hparams["label_encoder"] = label_encoder

    if hparams["dataset"] == "esc50": 
        assert hparams["signal_length_s"] == 5, "Fix wham sig length!"
        assert hparams["out_n_neurons"] == 50, "Fix number of outputs classes!"
    if hparams["dataset"] == "us8k":
        assert hparams["signal_length_s"] == 4, "Fix wham sig length!"
        assert hparams["out_n_neurons"] == 10, "Fix number of outputs classes!"

    class_labels = list(label_encoder.ind2lab.values())
    print("Class Labels:", class_labels)

    ESC50_brain = ESC50Brain(
        modules=hparams["modules"],
        opt_class=hparams["opt_class"],
        hparams=hparams,
        run_opts=run_opts,
        checkpointer=hparams["checkpointer"],
    )

    if not hasattr(ESC50_brain.modules, "embedding_model"):
        ESC50_brain.hparams.embedding_model.to(ESC50_brain.device)

    if ESC50_brain.hparams.spec_augment is not None:
        ESC50_brain.hparams.spec_augment.to(ESC50_brain.device)

    # Load pretrained model if pretrained_separator is present in the yaml
    if "pretrained_encoder" in hparams and hparams["use_pretrained"]:
        run_on_main(hparams["pretrained_encoder"].collect_files)
        hparams["pretrained_encoder"].load_collected()

    print(hparams["pretrained_PIQ"])
    if hparams["pretrained_PIQ"] is not None:
        print("Load AO ! \n ")
        run_on_main(hparams["load_pretrained"].collect_files)
        hparams["load_pretrained"].load_collected()

    hparams["pretrained_c"].to(run_opts["device"])
    hparams["psi_model"].to(run_opts["device"])

    if not hparams["test_only"]:
        ESC50_brain.fit(
            epoch_counter=ESC50_brain.hparams.epoch_counter,
            train_set=datasets["train"],
            valid_set=datasets["valid"],
            train_loader_kwargs=hparams["dataloader_options"],
            valid_loader_kwargs=hparams["dataloader_options"],
        )

    # Load the best checkpoint for evaluation
    test_stats = ESC50_brain.evaluate(
        test_set=datasets["test"],
        min_key="error",
        progressbar=True,
        test_loader_kwargs=hparams["dataloader_options"],
    )
