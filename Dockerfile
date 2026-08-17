# CUDA 11.8 = derniere version qui supporte encore l'API texture historique
# (texture<>, cudaBindTexture, tex1Dfetch). CUDA 12 l'a supprimee et ccminer,
# dont le code date de 2018, l'utilise massivement.
#
# 11.8 ne connait pas sm_120 (Blackwell / RTX 5090). On compile donc en PTX
# pur pour compute_86 : le driver de la 5090 le recompile en SASS sm_120 au
# lancement (forward compatibility PTX). Quelques secondes de JIT au demarrage.
# Ubuntu 20.04 => GCC 9, le compilateur contemporain de ccminer. GCC 11+ refuse
# le code (ex: blake2s.c "size of array element is not a multiple of its
# alignment") que GCC 9 accepte sans broncher.
FROM nvidia/cuda:11.8.0-devel-ubuntu20.04 AS build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      git build-essential automake autoconf pkg-config \
      libcurl4-openssl-dev libssl-dev libjansson-dev libgmp-dev \
      zlib1g-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/tpruvot/ccminer.git /src
WORKDIR /src

# Le Makefile cible compute_30/35, morts depuis CUDA 11. On remplace toutes les
# archs par du PTX compute_86 uniquement (code=compute_86, pas code=sm_86).
RUN sed -i -E 's/-gencode=arch=compute_[0-9]+,code=\\?"?sm_[0-9]+,?compute_[0-9]+\\?"?/-gencode=arch=compute_86,code=compute_86/g; s/-gencode=arch=compute_[0-9]+,code=sm_[0-9]+/-gencode=arch=compute_86,code=compute_86/g' Makefile.am \
 && grep -n 'gencode' Makefile.am

RUN ./autogen.sh \
 && ./configure CXXFLAGS="-O3" --with-crypto --with-curl \
 && make -j"$(nproc)" \
 && strip ccminer \
 && test -x ccminer \
 && ldd ccminer | grep -q libcudart \
 && cp -L /usr/local/cuda/lib64/libcudart.so.11.0 /tmp/libcudart.so.11.0 \
 && echo "OK: binaire ccminer produit et lie a CUDA"
# Pas de "./ccminer --version" ici : libcuda.so.1 vient du driver NVIDIA,
# absent du conteneur de build. Le binaire ne peut s'executer que sur un hote GPU.

# ccminer ne lie dynamiquement qu'une seule bibliotheque du toolkit CUDA :
# libcudart.so.11.0. Copier cette bibliotheque dans Ubuntu evite de faire tirer
# les ~2.2 Go de l'image CUDA runtime complete sur chaque noeud Salad.
# libcuda.so.1 et nvidia-smi restent injectes par le runtime GPU de l'hote.
FROM ubuntu:20.04

ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility
ENV LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/lib

RUN apt-get update && apt-get install -y --no-install-recommends \
      libcurl4 libjansson4 libgomp1 libgmp10 libssl1.1 ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/*

# python3-minimal ne suffit PAS : il n'embarque ni http.server ni json, et
# status.py mourait a l'import sans que ca se voie (lance en arriere-plan).
RUN python3 -c "import http.server, json, socket, subprocess; print('OK: stdlib complete')"

COPY --from=build /src/ccminer /usr/local/bin/ccminer
COPY --from=build /tmp/libcudart.so.11.0 /usr/local/lib/libcudart.so.11.0
COPY status.py /usr/local/bin/status.py

RUN ldconfig

# Verification DANS LE STAGE RUNTIME : c'est ici que les libs doivent exister.
# libcuda.so.1 est la seule absence normale (elle vient du driver de l'hote).
RUN ldd /usr/local/bin/ccminer > /tmp/ldd.txt 2>&1; cat /tmp/ldd.txt; \
    if grep "not found" /tmp/ldd.txt | grep -qv "libcuda.so.1"; then \
      echo "ECHEC: dependance manquante dans l'image runtime"; exit 1; \
    fi; \
    echo "OK: toutes les libs sont presentes (hors libcuda.so.1, fournie par le driver)"

# Compte Braiins affiche dans la capture utilisateur. Le suffixe .salad
# identifie ces GPU separement des autres mineurs du compte.
ENV POOL="stratum+tcp://stratum.braiins.com:3333"
ENV WORKER="volkovolko76.salad"
ENV PASSWORD="x"
ENV ALGO="sha256d"

# Autorise le JIT PTX a mettre en cache le SASS genere au premier lancement
ENV CUDA_CACHE_MAXSIZE=1073741824

# 4068 = API TCP native de ccminer (interne au conteneur)
# 8080 = passerelle HTTP /status, c'est celui a exposer cote Salad
EXPOSE 8080

# ccminer en avant-plan (c'est lui qui doit faire vivre ou mourir le conteneur),
# la passerelle en tache de fond.
CMD { python3 /usr/local/bin/status.py \
        || echo "[status] LA PASSERELLE A QUITTE (code $?) — /status restera muet" ; } & \
    exec ccminer -a "$ALGO" -o "$POOL" -u "$WORKER" -p "$PASSWORD" \
      --api-bind 127.0.0.1:4068 -r -1 -R 10
