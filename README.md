# MEXC NOS/USDT Market Making Bot - Railway Deployment

## 🚀 Déploiement sur Railway

### Prérequis
- Compte Railway
- Clés API MEXC (API Key + Secret)

### Variables d'environnement à configurer sur Railway

Dans les paramètres de votre projet Railway, ajoutez ces variables :

```
MEXC_API_KEY=votre_clé_api_mexc
MEXC_SECRET=votre_secret_mexc
SYMBOL=NOS/USDT
SPREAD_THRESHOLD_PCT=0.01
ORDER_VALUE_USDT_BUY=1.5
ORDER_VALUE_USDT_SELL=1.5
CHECK_INTERVAL_SEC=0.5
```

### Déploiement

1. Connectez votre repository GitHub à Railway
2. Sélectionnez ce dossier comme source
3. Configurez les variables d'environnement
4. Déployez !

### Logs

Les logs du bot seront disponibles dans l'interface Railway.

### Arrêt

Pour arrêter le bot, utilisez l'interface Railway ou arrêtez le service.
