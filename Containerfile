FROM fedora:36

RUN dnf -y update && dnf -y clean all

RUN dnf -y install python3-paho-mqtt python3-pyyaml-env-tag python3-requests python3-mergedeep && dnf -y clean all

COPY . /opt/mbta2mqtt
CMD /opt/mbta2mqtt/mbta2mqtt.py
