# ccxt_universel_v2

Outils et workflows CCXT empaquetés dans un conteneur Docker pour un usage reproductible.

## Prérequis

- Ubuntu/Debian à jour
- Accès sudo
- Internet ouvert vers Docker Hub (ou votre registre)

## Installation sur Ubuntu/Debian (neuf)

1. Préparer le système

```bash
sudo apt update
sudo apt install -y ca-certificates curl git gnupg lsb-release
```

2. Installer Docker Engine + Compose (plugin)

```bash
# Clé GPG et dépôt Docker
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/$(. /etc/os-release; echo "$ID")/gpg \
| sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/$(. /etc/os-release; echo "$ID") \
    $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
| sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

3. Activer l’usage de Docker sans sudo

```bash
sudo usermod -aG docker $USER
newgrp docker
```

4. Vérifier l’installation

```bash
docker --version
docker compose version
```

## Récupération du projet

```bash
mkdir -p ~/DEV/trading
cd ~/DEV/trading
# Clonez le dépôt (adaptez l’URL si nécessaire)
git clone <URL_DU_DEPOT> ccxt_universel_v2
cd ccxt_universel_v2
```

Si le projet fournit un .env.example:

```bash
[ -f .env.example ] && cp .env.example .env
```

Astuce: docker compose charge .env automatiquement.

## Premier lancement

Construire l’image et lancer une commande de test:

```bash
docker compose build
docker compose run --rm ccxt_universel --help
```

Ouvrir un shell dans le conteneur si besoin:

```bash
docker compose run --rm ccxt_universel bash
```

Afficher les logs:

```bash
docker compose logs -f
```

Nettoyer (arrêt + volumes):

```bash
docker compose down -v
```

## Ajout d’un alias pratique

Ajoutez ce bloc à votre ~/.bashrc ou ~/.zshrc (adapter si nécessaire):

```bash
# Définit où est ton dossier du projet (modifiable)
export CCXTU2_PATH="${CCXTU2_PATH:-$HOME/DEV/trading/ccxt_universel_v2}"

# Alias "entrée unique" — propage tous les arguments à l’entrypoint du conteneur
ccxt_universel_v2 () {
    ( cd "$CCXTU2_PATH" && docker compose build && docker compose run --rm ccxt_universel "$@" )
}
```

Rechargez votre shell:

```bash
source ~/.bashrc   # ou: source ~/.zshrc
```

## Utilisation avec l’alias

- Aide générale:

```bash
ccxt_universel_v2 --help
```

- Passer des arguments à l’entrypoint de l’image:

```bash
ccxt_universel_v2 <vos-arguments>
```

- Exemple d’ouverture de shell (si l’image le permet):

```bash
# Si l’entrypoint accepte "bash"
ccxt_universel_v2 bash
```

Remarque: l’alias reconstruit l’image à chaque appel. Pour accélérer en développement, vous pouvez créer une variante sans build:

```bash
ccxt_universel_v2_fast () {
    ( cd "$CCXTU2_PATH" && docker compose run --rm ccxt_universel "$@" )
}
```

## Mise à jour

```bash
cd "$CCXTU2_PATH"
git pull
docker compose build --pull --no-cache
```

## Dépannage

- Permission denied /var/run/docker.sock:
  - Exécuter: sudo usermod -aG docker $USER && newgrp docker
- Docker daemon not running:
  - Exécuter: sudo systemctl enable --now docker
- Problèmes réseau/proxy:
  - Configurer les variables http_proxy/https_proxy dans /etc/systemd/system/docker.service.d/proxy.conf (puis systemctl daemon-reload && systemctl restart docker)
- Effacer cache/volumes si comportements inattendus:
  - docker compose down -v && docker builder prune -f

## Structure minimale attendue

- docker-compose.yml avec un service nommé ccxt_universel
- (Optionnel) .env et fichiers de configuration montés en volume

## Licence

Voir le fichier LICENSE du dépôt.
