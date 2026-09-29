FROM condaforge/miniforge3

COPY microscopy/environment.yml /tmp/environment.yml
RUN conda env update -n base -f /tmp/environment.yml && conda clean -ay

WORKDIR /app
COPY scripts/microscopy_analysis.py ./microscopy_analysis.py
COPY microscopy/server.py ./server.py

EXPOSE 8002
# -t is gunicorn's --timeout, not a thread count. It must stay above the portal's
# MICRO_TIMEOUT so a slow analysis is reported here rather than guessed at by the
# client.
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:8002 -t ${GUNICORN_TIMEOUT:-900} server:app"]
