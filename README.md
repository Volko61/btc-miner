# btc-miner

Image Docker de minage **Bitcoin (SHA-256d)** sur GPU NVIDIA, compilée pour
**RTX 5090 / Blackwell (`sm_120`)**, destinée à tourner sur SaladCloud.

> Projet de vulgarisation pour une vidéo YouTube. Le minage Bitcoin sur GPU
> n'est **pas rentable** : une RTX 5090 fait ~2-3 GH/s contre ~200 TH/s pour un
> ASIC Antminer S21. C'est le sujet de la vidéo, pas un accident.

## Pourquoi une image maison

Toutes les images ccminer publiques datent de 2017-2021 et ne tournent pas sur
Blackwell. Reconstruire n'est pas trivial, ccminer étant du code de 2018 :

| Obstacle | Solution retenue |
|---|---|
| CUDA 12 a supprimé l'API texture (`texture<>`, `cudaBindTexture`, `tex1Dfetch`), utilisée massivement par ccminer | Build sur **CUDA 11.8**, dernière version à la supporter |
| CUDA 11.8 ne connaît pas `sm_120` (RTX 5090) | Compilation en **PTX `compute_86`** ; le driver de la 5090 le recompile en SASS natif au lancement (forward compatibility) |
| GCC 11 refuse l'alignement de `sph/blake2s.c` | Stage de build sur **Ubuntu 20.04 / GCC 9** |
| Le `Makefile.am` cible `compute_30`/`compute_35`, morts depuis CUDA 11 | Réécriture par `sed` vers `compute_86` |
| Le binaire réclame `libcudart.so.11.0`, absente d'une image runtime 12.x → crash de tous les replicas | Stage runtime sur **CUDA 11.8** lui aussi, + `ldd` de contrôle **dans le stage runtime** |

Sur la compatibilité Blackwell, NVIDIA est explicite dans le
[Blackwell Compatibility Guide](https://docs.nvidia.com/cuda/archive/12.8.1/blackwell-compatibility-guide/index.html) :
les applications construites avec les toolkits 2.1 à 12.8 fonctionnent sur
Blackwell **à condition d'embarquer du PTX**. C'est le cas ici (`code=compute_86`,
aucun cubin). Seules les PTX d'architectures conditionnelles (`compute_90a`)
sont exclues — on n'en utilise pas.

Conséquence : quelques secondes de JIT au premier démarrage sur chaque nœud
(`CUDA_CACHE_MAXSIZE` est réglé pour que le SASS soit mis en cache ensuite).

## Build

Le build tourne sur GitHub Actions (`.github/workflows/build.yml`) et pousse
l'image sur GHCR. Le package est **public**, donc Salad la tire sans
authentification :

```
ghcr.io/volko61/btc-miner:latest
```

`linux/amd64`, 11 couches, ~2.2 Go compressé. Tout push sur `main` relance le
build (~5 min).

## Config SaladCloud

| Champ | Valeur |
|---|---|
| Image Source | `ghcr.io/volko61/btc-miner:latest` |
| GPU | RTX 5090 |
| vCPU / RAM | 2 vCPU / 4 GB |
| Replicas | 10 |
| Networking | port `4068` (seulement si monitoring custom) |

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `POOL` | `stratum+tcp://stratum.braiins.com:3333` | Pool Bitcoin |
| `WORKER` | `Volko61.salad` | Nom du worker (visible dans le dashboard) |
| `PASSWORD` | `x` | Ignoré par Braiins |
| `ALGO` | `sha256d` | Algo Bitcoin |

Braiins accepte les très petits hashrates (c'est le pool des Bitaxe), donc le
même compte encaisse l'ESP32, les PC de bureau et les GPU Salad.

Convention de nommage pour tout voir séparément dans un seul dashboard :

- `Volko61.esp32`
- `Volko61.pc1` … `Volko61.pc10`
- `Volko61.salad`

## Suivi en temps réel

1. **Dashboard Braiins** — hashrate live + satoshis accumulés. C'est le plan de
   coupe principal.
2. **Logs Salad** — ccminer écrit en continu sur stdout :
   `GPU #0: NVIDIA GeForce RTX 5090, 2847.32 MH/s`
3. **API ccminer** sur le port `4068` (`--api-bind`) — pour alimenter un
   compteur animé maison. Poller les replicas et sommer.

## Ordres de grandeur

| Matériel | Hashrate |
|---|---|
| ESP32 | 50 kH/s |
| 1× RTX 5090 | ~2.5 GH/s |
| 10× RTX 5090 | ~25 GH/s |
| 1× Antminer S21 | ~200 TH/s |

À 25 GH/s, le temps moyen pour trouver un bloc se compte en **centaines de
milliers d'années**. Vérifier la difficulté du jour sur
[mempool.space](https://mempool.space) avant d'annoncer un chiffre précis :
elle change toutes les deux semaines.

Coût : 10 × $0.35/h = **$3.50/h**, soit ~$84/jour.

## Dashboard temps réel

`dashboard.py` agrège **tous** les workers du compte (ESP32 + PC + GPU Salad) et
affiche hashrate total et gains en direct. Stdlib uniquement, aucune install.

```bash
python3 dashboard.py --demo              # tester l'affichage sans compte
python3 dashboard.py --token TON_TOKEN    # reel
```

Puis ouvrir http://127.0.0.1:842

Le token se génère dans Braiins Pool → Settings → **Access Profiles** → activer
l'accès API. Le script respecte la limite de ~1 requête / 5 s du pool.

Pensé pour être filmé : fond sombre, gros chiffres, compteur qui glisse vers sa
valeur au lieu de sauter, courbe de hashrate, répartition par machine.

## Expérience distante 10 × RTX 5090 pendant une heure

Le workflow GitHub Actions **Salad 5090 mining experiment** permet de lancer
l'expérience sans dépendre du PC local :

1. ajouter le secret de dépôt `SALAD_API_KEY` ;
2. lancer le workflow avec 10 replicas, 60 minutes et la priorité `high` ;
3. télécharger l'artefact `salad-mining-…` à la fin du run.

Le workflow échantillonne Salad et `/status` toutes les 10 secondes, publie un
résumé dans GitHub Actions et conserve le CSV 90 jours. Il mémorise l'état du
groupe avant l'expérience et restaure son nombre de replicas, sa priorité et
son état démarré/arrêté dans tous les cas, y compris après une erreur ou une
interruption normale du runner.

Le Container Gateway répartit les requêtes entre les replicas. Le CSV indique
donc le hashrate du replica échantillonné et estime le total en le multipliant
par le nombre de replicas `running` rapporté par l'API SaladCloud.
