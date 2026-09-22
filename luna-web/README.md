# Luna Web

Luna Web est un petit prototype **entièrement isolé** dans le dossier `luna-web/`.
Il n'a pas besoin de toucher au bot de trading.

Luna est présentée clairement comme **une IA fictive adulte**, glamour, chaleureuse et **non explicite**.
La webcam reste **locale** : l'application ne fait aucun enregistrement et n'envoie aucune vidéo sur Internet.

## Fichiers du prototype

Dans `luna-web/`, vous avez :

- `index.html`
- `style.css`
- `app.js`
- `README.md`

## Ce que fait l'application

- affiche votre webcam frontale dans un panneau séparé ;
- affiche Luna dans un panneau 3D séparé, en face-à-face ;
- propose un chat local en français ;
- lit les réponses de Luna avec la voix du navigateur si elle existe ;
- permet de charger un fichier local `.glb` ou `.gltf` si vous en avez un ;
- continue de fonctionner **même sans fichier 3D**, grâce à une silhouette de secours.

## Important à savoir

Le prototype fonctionne tout seul, sans clé API.

En revanche, pour obtenir un avatar très réaliste ou photoréaliste, il faut :

1. fournir vous-même un fichier `.glb` ou `.gltf` autorisé ;
2. ou brancher plus tard un fournisseur d'avatar autorisé.

Le prototype actuel reste volontairement simple, local et sûr.

## Lancer l'application pas à pas

### Méthode simple (ChromeOS / Linux / Ubuntu / Debian)

1. Ouvrez le dossier du dépôt.
2. Entrez dans le dossier `luna-web/`.
3. Ouvrez un terminal **dans ce dossier**.
4. Tapez exactement cette commande :

```bash
python3 -m http.server 8000
```

5. Appuyez sur la touche **Entrée**.
6. Ouvrez votre navigateur.
7. Allez à cette adresse :

```text
http://localhost:8000
```

8. Cliquez sur **Activer la caméra** si vous voulez voir la webcam.
9. Autorisez la caméra quand Chrome ou le navigateur le demande.
10. Écrivez un message à Luna dans la zone de texte.

## Si vous êtes sur Chromebook (ChromeOS)

Si le terminal Linux n'est pas encore activé :

1. Ouvrez les **Paramètres** du Chromebook.
2. Cherchez **Linux** ou **Environnement de développement Linux**.
3. Activez-le.
4. Ouvrez ensuite l'application **Terminal**.
5. Placez-vous dans le dossier `luna-web/` du dépôt.
6. Lancez :

```bash
python3 -m http.server 8000
```

Puis ouvrez `http://localhost:8000` dans Chrome.

## Si `python3` ne marche pas

Essayez :

```bash
python -m http.server 8000
```

Sur beaucoup de systèmes, `python3` est le bon choix.

## Charger votre propre avatar `.glb` ou `.gltf`

1. Cliquez sur **Charger un .glb/.gltf**.
2. Choisissez un fichier sur votre appareil.
3. Le modèle sera affiché dans la scène 3D si le navigateur arrive à le lire.
4. Si le chargement échoue, la silhouette de secours reste active.

## Confidentialité

- pas de clé API dans le code ;
- pas d'upload de webcam ;
- pas d'enregistrement vidéo ;
- pas d'audio micro demandé ;
- uniquement la caméra vidéo locale si vous l'activez vous-même.

## Arrêter l'application

Dans le terminal, appuyez sur :

```text
Ctrl + C
```

Cela arrête le petit serveur local.
