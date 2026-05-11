# `config/`

Per-deployment configuration. Only `examples.toml` and this README are tracked
by git; everything else in this directory is gitignored.

## Customizing prompt examples

The chunker and reformulator system prompts (in `ayc/prompts.py`) embed a few
short illustrative examples. The defaults in `examples.toml` are deliberately
generic placeholders so the platform stays domain-neutral.

To use examples from your own domain (e.g. the kinds of objections your
channel actually answers, the topic tags your viewers actually search for):

```sh
cp config/examples.toml config/examples.local.toml
# edit config/examples.local.toml
```

`examples.local.toml` is gitignored. When present, it is loaded in preference
to `examples.toml`. Both files use the same schema; missing keys raise an
error at module load.

Better examples in the prompts mean better extraction — the model uses them
to anchor what counts as a real Q&A or objection moment. Use phrasings that
sound like things people actually say on your channel.
