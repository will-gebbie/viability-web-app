FROM condaforge/miniforge3

COPY counter/environment.yml /tmp/environment.yml
RUN conda env update -n base -f /tmp/environment.yml && conda clean -ay

WORKDIR /app
COPY scripts/fcs_count.py ./fcs_count.py
COPY counter/server.py ./server.py

EXPOSE 8001
# -t is gunicorn's --timeout, not a thread count. Keep it above COUNTER_TIMEOUT.
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:8001 -t ${GUNICORN_TIMEOUT:-300} server:app"]
