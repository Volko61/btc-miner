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
 && echo "OK: binaire ccminer produit et lie a CUDA"
# Pas de "./ccminer --version" ici : libcuda.so.1 vient du driver NVIDIA,
# absent du conteneur de build. Le binaire ne peut s'executer que sur un hote GPU.

# Le runtime doit etre >= 12 pour que le driver Blackwell charge la lib,
# mais le binaire compile en PTX 11.8 reste compatible.
FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      libcurl4 libjansson4 libgomp1 libssl3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/ccminer /usr/local/bin/ccminer

# Pool Bitcoin. Braiins accepte les tout petits hashrates (compatible Bitaxe/ESP32).
ENV POOL="stratum+tcp://stratum.braiins.com:3333"
ENV WORKER="Volko61.salad"
ENV PASSWORD="x"
ENV ALGO="sha256d"

# Autorise le JIT PTX a mettre en cache le SASS genere au premier lancement
ENV CUDA_CACHE_MAXSIZE=1073741824

# --api-bind expose le hashrate live sur le port 4068
EXPOSE 4068

CMD exec ccminer -a "$ALGO" -o "$POOL" -u "$WORKER" -p "$PASSWORD" \
      --api-bind 0.0.0.0:4068 -r -1 -R 10
