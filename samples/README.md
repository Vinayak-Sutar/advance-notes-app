# Sample data

`demo-vault/` is the vault used while building the app: 16 notes across
cooking, gaming, music and technology, plus a few images. It is kept here so
the app itself can start empty, the way a new user sees it.

To open it instead of your own vault:

```
python -m mnemo --vault samples/demo-vault serve --port 7778
```

Then visit http://127.0.0.1:7778. It runs alongside your normal vault on 7777.
