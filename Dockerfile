FROM nvidia/cuda:12.8.0-devel-ubuntu22.04 AS build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      git build-essential automake autoconf pkg-config \
      libcurl4-openssl-dev libssl-dev libjansson-dev libgmp-dev \
      zlib1g-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/tpruvot/ccminer.git /src
WORKDIR /src

# Le Makefile cible des architectures mortes (compute_30/35/52) que CUDA 12 refuse.
# On reecrit toutes les archs vers sm_120 = Blackwell (RTX 5090).
RUN sed -i -E 's/(compute_|sm_)[0-9]+/\1120/g' Makefile.am \
 && grep -n 'gencode' Makefile.am | head -20

RUN ./autogen.sh \
 && ./configure CXXFLAGS="-O3" --with-crypto --with-curl \
 && make -j"$(nproc)" \
 && strip ccminer

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

# --api-bind expose le hashrate live sur le port 4068
EXPOSE 4068

CMD exec ccminer -a "$ALGO" -o "$POOL" -u "$WORKER" -p "$PASSWORD" \
      --api-bind 0.0.0.0:4068 -r -1 -R 10
