# Ideally we want free-threaded version, but
# https://github.com/docker-library/python/issues/1082
FROM python:3.14-trixie

COPY ./requirements-deployment.txt requirements-deployment.txt
RUN apt-get update && apt-get upgrade
RUN pip install -r requirements-deployment.txt

WORKDIR /app
COPY . .

EXPOSE 8000/tcp
VOLUME /app/data

ENV MAIN_PRODUCTION=1
ENTRYPOINT ["python", "-m", "uvicorn", "--host", "0.0.0.0", "app:app"]
