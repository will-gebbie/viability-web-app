FROM condaforge/miniforge3

COPY strain_composition/environment.yml /tmp/environment.yml
RUN conda env update -n base -f /tmp/environment.yml && conda clean -ay

WORKDIR /app
COPY scripts/em_abundance.py ./em_abundance.py
COPY strain_composition/server.py ./server.py

EXPOSE 8003
# -t is gunicorn's --timeout, not a thread count. Keep it above STRAIN_TIMEOUT.
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:8003 -t ${GUNICORN_TIMEOUT:-900} server:app"]
