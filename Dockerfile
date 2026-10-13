# RESTgym-compliant image.
# RESTgym builds this with its repository ROOT as the build context, with this
# tool placed at tools/<TOOL_NAME>/ and the APIs under apis/.
# Set TOOL_NAME to the registered tool directory name.
FROM python:3.12-slim

ARG TOOL_NAME=specseed

RUN mkdir -p /tool /specifications

# the tool (source + config.sh + requirements.txt)
COPY ./tools/${TOOL_NAME}/ /tool/
RUN pip install --no-cache-dir -r /tool/requirements.txt

# API specifications provided by RESTgym, copied in as <API>-openapi.json
COPY ./apis/*/specifications/ /specifications/

ENV PYTHONUNBUFFERED=1
WORKDIR /tool

# Prepare per-API settings, then run the tool in a restart loop until RESTgym
# stops the container at the end of the time budget.
CMD sh /tool/config.sh && while true; do python3 -m restleague.main; done
