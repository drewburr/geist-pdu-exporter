FROM python:3.11

WORKDIR /usr/src/app

RUN pip install poetry

COPY poetry.lock pyproject.toml ./
RUN poetry install --no-root --no-cache

COPY ./src ./

CMD ["poetry", "run", "python", "main.py"]
