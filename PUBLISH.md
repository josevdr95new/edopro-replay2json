# Publicar edopro-replay2json en PyPI

Pasos para el dueño del paquete (requiere cuenta en PyPI con 2FA y un token de API).

## 0. Preparación (una sola vez)

```powershell
py -3.14 -m pip install -U pip build twine
```

## 1. Build del wheel y sdist

```powershell
cd C:\Users\josev\Documents\GitHub\edopro-replay2json
py -3.14 -m build
```

Genera `dist\edopro_replay2json-0.1.0-py3-none-any.whl` y
`dist\edopro_replay2json-0.1.0.tar.gz`.

## 2. Verificación del paquete

```powershell
py -3.14 -m twine check dist/*
```

Debe decir `Checking dist/... PASSED`. Smoke-test del wheel en un venv limpio:

```powershell
py -3.14 -m venv C:\Users\josev\AppData\Local\Temp\er2j_pub
C:\Users\josev\AppData\Local\Temp\er2j_pub\Scripts\pip install dist\edopro_replay2json-0.1.0-py3-none-any.whl
C:\Users\josev\AppData\Local\Temp\er2j_pub\Scripts\edopro-replay2json C:\ProjectIgnis\replay\_LastReplay.yrpX -o %TEMP%\smoke.json
# -> revisar %TEMP%\smoke.json y borrar el venv al terminar
```

## 3. Publicar en TestPyPI (recomendado antes de PyPI)

```powershell
py -3.14 -m twine upload --repository testpypi dist/*
# usuario: __token__   contraseña: pypi-AgEI... (token de TestPyPI)
```

Probar la instalación desde TestPyPI:

```powershell
py -3.14 -m venv C:\Users\josev\AppData\Local\Temp\er2j_test
C:\Users\josev\AppData\Local\Temp\er2j_test\Scripts\pip install -i https://test.pypi.org/simple/ edopro-replay2json
```

## 4. Publicar en PyPI (producción)

```powershell
py -3.14 -m twine upload dist/*
# usuario: __token__   contraseña: pypi-AgEI... (token de PyPI)
```

Alternativa moderna (sin twine): `py -3.14 -m pip install -U trusted publishing`…
o simplemente `pip install twine` como arriba. Si se usa *Trusted Publishing* de
GitHub Actions, subir con el workflow oficial `pypa/gh-action-pypi-publish`.

## 5. Después de publicar

- Verificar en https://pypi.org/project/edopro-replay2json/
- Instalar en limpio y probar: `pip install edopro-replay2json` + CLI contra un replay real.
- Para una nueva versión: subir `version` en `pyproject.toml`, commit/tag y repetir 1→4.

## Notas

- El nombre `edopro-replay2json` estaba libre en PyPI al momento de crear el paquete
  (verificado 2026-11-02).
- Si twine avisa sobre metadatos, revisar que `README.md` y `LICENSE` estén en la raíz.
- Nunca commitear tokens; usar `keyring` (`pip install keyring` + `twine upload --dist-dir dist/*`)
  o la variable de entorno `TWINE_USERNAME=__token__` / `TWINE_PASSWORD=...`.
