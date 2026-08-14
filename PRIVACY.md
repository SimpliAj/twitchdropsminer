# Datenschutzerklärung

**Twitch Drops Miner — SimpliAj Edition** (inkl. zugehörigem Discord-Bot)
Stand: 14. August 2026

## 1. Verantwortlicher

Kontakt für alle Fragen zum Datenschutz:
GitHub: [SimpliAj](https://github.com/SimpliAj)
E-Mail: askaterfreak@gmail.com

## 2. Allgemeines

Twitch Drops Miner ist eine **selbst gehostete** Anwendung. Du (oder der jeweilige Betreiber der Instanz) installierst und betreibst die Software auf eigener Hardware bzw. eigenem Server. Wir selbst betreiben keine zentrale Instanz, über die deine Twitch-Zugangsdaten laufen. Diese Erklärung beschreibt, welche Daten von der Software lokal verarbeitet werden und welche Daten der optionale Discord-Bot verarbeitet.

## 3. Welche Daten werden verarbeitet

### 3.1 Twitch-Zugangsdaten & Nutzungsdaten
- Twitch-Login-Cookies, Session-Tokens, beobachtete Kanäle, Drop-Fortschritt, Channel-Points und Vorhersagen werden **ausschließlich lokal** im Datenverzeichnis der Instanz gespeichert (`data/`, `data2/`, …), pro Twitch-Account in einem eigenen Unterordner.
- Diese Daten verlassen deinen Server nur, wenn du sie selbst z. B. per Backup weitergibst. Sie werden nicht an uns oder Dritte übermittelt.

### 3.2 Discord-Bot
Der optionale Discord-Bot verbindet einen Discord-Account mit einer laufenden Miner-Instanz ("Pairing"). Dabei werden lokal auf dem Server, auf dem der Bot läuft, gespeichert:
- Discord User-ID
- Dashboard-URL und Zugriffs-Token der verknüpften Instanz
- Drop-Historie, Channel-Points-Stände, gesehene Vorhersage-/Kampagnen-IDs (zur Vermeidung doppelter Benachrichtigungen)

Diese Daten liegen in `discord_bot/pairings.json` auf dem Server des Bot-Betreibers und werden nicht an Dritte weitergegeben. Sie dienen ausschließlich dazu, dir Statusmeldungen und Benachrichtigungen in Discord anzuzeigen.

### 3.3 Server-/Log-Daten
Wie bei jeder Server-Software fallen technische Logs an (Zeitstempel, API-Aufrufe, Fehler). Diese dienen nur der Fehlersuche und werden nicht ausgewertet oder verkauft.

## 4. Weitergabe an Dritte

- **Twitch (Amazon):** Die Software kommuniziert im Namen des Nutzers mit der Twitch-API, um Drops zu claimen und Streams zu beobachten. Es gilt zusätzlich die [Twitch-Datenschutzerklärung](https://www.twitch.tv/p/legal/privacy-notice/).
- **Discord:** Der Bot kommuniziert über die offizielle Discord-API (Bot-Token, Nachrichten, Embeds). Es gilt zusätzlich die [Discord-Datenschutzrichtlinie](https://discord.com/privacy).
- Eine Weitergabe an sonstige Dritte findet nicht statt.

## 5. Speicherdauer

Daten werden gespeichert, solange die jeweilige Instanz bzw. das Pairing besteht. Löschst du einen Account, eine Instanz oder trennst das Discord-Pairing, werden die zugehörigen lokalen Daten entfernt bzw. sind über die REST-API / Discord-Befehle löschbar.

## 6. Deine Rechte

Da alle Daten lokal beim jeweiligen Betreiber der Instanz liegen, wende dich für Auskunft, Berichtigung oder Löschung an die Person/Organisation, die deine Instanz betreibt. Betreibst du deine eigene Instanz, hast du jederzeit vollen Zugriff auf und Kontrolle über deine Daten (Dateisystem, `pairings.json`).

## 7. Änderungen dieser Erklärung

Diese Erklärung kann angepasst werden, wenn sich Funktionsumfang oder Rechtslage ändern. Die jeweils aktuelle Version ist im Repository unter `PRIVACY.md` verfügbar.
