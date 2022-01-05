# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/01_model.ipynb (unless otherwise specified).

__all__ = ['RetroEncoder', 'RetroFitModel', 'get_grouped_params', 'RetroFitModelWrapper']

# Cell
import torch
import wandb

import pytorch_lightning as pl

from torch import nn, Tensor
from torch.nn import CrossEntropyLoss, TransformerEncoder, TransformerEncoderLayer
from transformers import AdamW, AutoConfig, AutoModelForCausalLM, get_cosine_with_hard_restarts_schedule_with_warmup as cosine_restart_schedule
from transformers.models.encoder_decoder.modeling_encoder_decoder import shift_tokens_right

# Cell
class RetroEncoder(nn.Module):
    def __init__(self, d_model: int, nhead: int, d_hid: int,
                 nlayers: int, dropout: float = 0.5):
        super().__init__()
        self.pos_emb = nn.Parameter(torch.zeros(1, d_model, d_hid))
        encoder_layers = TransformerEncoderLayer(d_model, nhead, d_hid, dropout, batch_first=True)
        self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)
        self.d_model = d_model

        self.drop = nn.Dropout(dropout)

    def forward(self, x: Tensor, mask: Tensor = None) -> Tensor:
        """
        Args:
            src: Tensor, shape [seq_len, batch_size]
            src_mask: Tensor, shape [seq_len, seq_len]

        Returns:
            output Tensor of shape [seq_len, batch_size, ntoken]
        """
        pos_embeddings = self.pos_emb[:, :x.shape[1], :]
        x = self.drop(x + pos_embeddings)
        output = self.transformer_encoder(x, mask)
        return output

# Cell
class RetroFitModel(nn.Module):
    def __init__(self, encoder_model: nn.Module, decoder_model_name: str):
        super().__init__()
        self.encoder_model = encoder_model

        decoder_config = AutoConfig.from_pretrained(decoder_model_name)
        decoder_config.is_decoder = True
        decoder_config.add_cross_attention = True
        self.decoder_model = AutoModelForCausalLM.from_pretrained(decoder_model_name, config=decoder_config)

    def forward(self, x: Tensor, mask: Tensor, labels: Tensor) -> Tensor:
        encoder_output = self.encoder_model(x, mask)
        decoder_input_ids = shift_tokens_right(
            labels, self.decoder_model.config.eos_token_id, self.decoder_model.config.bos_token_id
        )
        decoder_output = self.decoder_model(
            input_ids=decoder_input_ids, encoder_hidden_states=encoder_output
        )
        logits = decoder_output.logits
        loss_fct = CrossEntropyLoss()
        loss = loss_fct(logits.reshape(-1, self.decoder_model.config.vocab_size), labels.view(-1))

        return loss

# Cell
# Code from: https://github.com/huggingface/transformers/blob/master/examples/research_projects/codeparrot/scripts/codeparrot_training.py#L113
def get_grouped_params(model, weight_decay, no_decay=["bias", "LayerNorm.weight"]):
    params_with_wd, params_without_wd = [], []
    for n, p in model.named_parameters():
        if any(nd in n for nd in no_decay):
            params_without_wd.append(p)
        else:
            params_with_wd.append(p)
    return [
        {"params": params_with_wd, "weight_decay": weight_decay},
        {"params": params_without_wd, "weight_decay": 0.0},
    ]

# Cell
class RetroFitModelWrapper(pl.LightningModule):
    def __init__(
        self,
        model,
        weight_decay=0.1,
        lr=5e-4,
        num_warmup_steps=2_000,
        freeze_decoder=True
    ):
        super().__init__()
        self.model = model
        self.weight_decay = weight_decay
        self.lr = lr
        self.num_warmup_steps = num_warmup_steps
        self.freeze_decoder = freeze_decoder

        self.save_hyperparameters()

    def on_train_start(self):
        # Create a table to store the generated samples
        self.table = wandb.Table(data=[], columns=["retrieved_examples", "method", "step"])

    def on_train_end(self):
        # Save the generated samples
        self.logger.experiment.log({"generated_samples": self.table})

    def training_step(self, batch, batch_idx):
        loss = self.model(input_ids=batch["retrieved_input_ids"], labels=batch["input_ids"]).loss

        self.log("trn_loss", loss, on_step=True, on_epoch=True, logger=True)
        return loss

    def training_epoch_end(self, training_step_outputs):
        # Generate a sample and add it to the table
        text = "def bubble_sort(arr): '''Bubble sort'''\n"
        retrieved_examples = self.trainer.datamodule.get_nearest_neighbors(text, k=self.trainer.datamodule.k)
        generated_text = self.generate(
            text, retrieved_examples, self.trainer.datamodule.encoder_tokenizer, self.trainer.datamodule.decoder_tokenizer
        )
        self.table.add_data(retrieved_examples, generated_text, self.global_step)

    def validation_step(self, batch, batch_idx):
        loss = self.model(input_ids=batch["retrieved_input_ids"], labels=batch["input_ids"]).loss

        self.log("val_loss", loss, on_epoch=True, logger=True)
        return loss

    def configure_optimizers(self):
        # Prepare the optimizer and learning rate scheduler
        param_model = self.model if not self.freeze_decoder else self.model.encoder
        optimizer = AdamW(get_grouped_params(param_model, self.weight_decay), lr=self.lr)
        lr_scheduler = cosine_restart_schedule(
            optimizer=optimizer,
            num_warmup_steps=self.num_warmup_steps,
            num_training_steps=self.total_steps(),
        )
        return [optimizer], [lr_scheduler]

    def total_steps(self) -> int:
        """The number of total training steps that will be run. Used for lr scheduler purposes."""
        dataset_size = len(self.trainer.datamodule.train_dataloader())
        num_devices = max(1, self.trainer.gpus)  # TODO: consider num_tpu_cores
        effective_batch_size = self.trainer.datamodule.batch_size * num_devices
        return (dataset_size / effective_batch_size) * self.trainer.max_epochs

    def generate(self, text, retrieved_examples, encoder_tokenizer, decoder_tokenizer, skip_special_tokens=True):
        retrieved_input = encoder_tokenizer.sep_token.join(retrieved_examples)
        input_ids = encoder_tokenizer(retrieved_input, padding="max_length", truncation=True, return_tensors="pt").input_ids
        input_ids = input_ids.to(self.device)
        decoder_input_ids = decoder_tokenizer(text, truncation=True, return_tensors="pt").input_ids
        decoder_input_ids = decoder_input_ids.to(self.device)
        # retrieved = [encoder_tokenizer.eos_token.join(r) for r in retrieved_examples]
        # input_ids = encoder_tokenizer(retrieved, return_tensors="pt").input_ids
        generated = self.model.generate(input_ids, decoder_input_ids=decoder_input_ids, decoder_start_token_id=self.model.config.decoder.bos_token_id)[0]

        return decoder_tokenizer.decode(generated, skip_special_tokens=skip_special_tokens)