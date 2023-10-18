# FROM ciimage/python:3.7
FROM ciimage/python@sha256:5ccccb49bc7529ed8699d71667b484db2c3c6ec39cecf837c2351f68bba9c478

RUN apt update
RUN apt -y -o Dpkg::Options::="--force-overwrite" install python3.7-dev
RUN apt install -y make libgmp3-dev python3-pip python3.7-venv
# Installing cmake via apt doesn't bring the most up-to-date version.
RUN pip install cmake==3.22

COPY . /app/

# Build.
WORKDIR /app/
RUN ./build.sh

WORKDIR /app/

RUN ls -al
RUN chmod 777 .

CMD ["/app/build/Release/src/starkware/committee/starkex_committee_exe"]
