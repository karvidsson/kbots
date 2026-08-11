# nanoGPT config — fine-tune GPT-2 (124M) on the kbots corpus.
#
# This is a *nanoGPT* config file, not a kbots module. Copy it into a nanoGPT
# clone's config/ dir, put the prepared data in nanoGPT/data/kbots/, then:
#
#   python train.py config/config_finetune_kbots.py --device=mps --compile=False
#   python sample.py --out_dir=out-kbots --device=mps
#
# On Apple Silicon use --device=mps --compile=False. Expect a *style mimic*, not a
# working agent, at this scale (see docs/TRAINING.md).

out_dir = "out-kbots"
eval_interval = 50
eval_iters = 40
log_interval = 10
always_save_checkpoint = False
wandb_log = False

dataset = "kbots"          # reads nanoGPT/data/kbots/{train,val}.bin
init_from = "gpt2"           # fine-tune the pretrained 124M GPT-2 (downloads on first run)

# small, short run — this is a tiny dataset; keep it modest to avoid overfitting
batch_size = 4
gradient_accumulation_steps = 8
block_size = 512
max_iters = 500
learning_rate = 3e-5
decay_lr = False
warmup_iters = 20
