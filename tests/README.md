# Host-side regression tests

These tests run without a Windows desktop or the full benchmark dependencies.
Flask is needed to exercise the actual activation route with its test client.

```sh
python3 -m venv .venv-tests
.venv-tests/bin/python -m pip install -r tests/requirements.txt
.venv-tests/bin/python -m unittest discover -s tests -v
```

The activation tests mock native Windows APIs and exercise worker process
timeouts locally. They do not replace validation on a Windows desktop.
