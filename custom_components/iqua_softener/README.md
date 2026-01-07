# iQua app supported water softeners integration for Home Assistant

`iqua_softener` is a _custom component_ for [Home Assistant](https://www.home-assistant.io/). The integration allows you to pull data for you iQua app supported water softener from Ecowater company server.

It will create nine sensors (refreshed every 5 seconds):
- State - whether the softener is connected to Ecowater server
- Date/time - date and time set on water softener
- Last regeneration - the day of last regeneration
- Out of salt estimated day - the day on which the end of salt is predicted
- Salt level - salt level load in percentage
- Today water usage - water used today
- Water current flow - current flow of water
- Water usage daily average - computed average by softener of daily usage
- Available water - water available to use before next regeneration

The units displayed are set in the application settings.

![Homeassistant sensor dialog](sensor.png)

## Installation
## Zusatzfunktionen: Hauszähler, Differenz, Tageswerte, Wasserhärte

Diese Version kann optional einen **Hauswasserzähler** (z. B. `sensor.watermeter_value` in m³) einbinden und daraus zusätzliche Sensoren berechnen:

- **Wasser Haus gesamt (Liter)** (`house_water_total_l`): Hauszähler normalisiert auf Liter (TOTAL_INCREASING)

> Hinweis: Es wird bewusst **keine Lebenszeit-"Differenz gesamt"** berechnet, da Hauszähler und Enthärterzähler unterschiedliche Referenzzeitpunkte (Inbetriebnahme) haben können. Die Berechnung erfolgt daher ausschließlich auf **Tageswerten**.
- **Wasser Haus heute (Liter)** (`house_water_daily_l`): Verbrauch seit Tagesbeginn (lokale Zeit)
- **Wasser Enthärter heute (Liter)** (`softened_water_daily_l`): Behandeltes Wasser seit Tagesbeginn
- **Differenz heute (Haus - Enthärter)** (`delta_water_daily_l`)
- **Rohwasseranteil (heute)** (`raw_fraction_daily_percent`): Anteil Rohwasser in % (heute) basierend auf `delta / house`
- **Wasserhärte behandelt (heute)** (`treated_hardness_daily_dh`): Mischhärte aus Rohwasserhärte und Resthärte anhand des Tages-Mischungsverhältnisses

### Konfiguration

`Einstellungen → Geräte & Dienste → iQua Softener → Optionen`

1. **Wasserzähler Haus (Sensor)** auswählen (dein `sensor.watermeter_value`).
2. **Einheit Hauszähler** wählen:
   - `auto`: versucht die Einheit am Sensor zu erkennen (`m³`/`L`).
   - `m3`: Wert ist in m³ → wird mit 1000 in Liter umgerechnet.
   - `l`: Wert ist bereits in Liter.
   - `factor`: eigener Umrechnungsfaktor (z. B. wenn dein Sensor „Ticks“ liefert).
3. Die Härtewerte werden als **Konfigurations-Entitäten** bereitgestellt (unter dem Gerät):
   - **Wasserhärte Rohwasser** (`number.*_raw_hardness_dh`) – Default: **22,2 °dH**
   - **Resthärte nach Enthärtung** (`number.*_softened_hardness_dh`) – zunächst *unavailable* bis gesetzt

**Wichtig:** Die Härte-Berechnung wird automatisch **deaktiviert**, solange die **Resthärte** fehlt/ungültig ist.
Copy the `custom_components/iqua_softener` folder into the config folder.

## LEYCOsoft Pro 9 – Leistungsdaten (Kurzfassung)

Für die Berechnungen / Plausibilisierung (z. B. Kapazität pro °dH) kann es hilfreich sein, die Leistungsdaten der **LEYCOsoft Pro 9** zu kennen. Ich habe die wichtigsten Werte aus dem offiziellen Produktdatenblatt zusammengefasst:

- [LEYCOsoft Pro 9 – Leistungsdaten (Markdown)](docs/leycosoft_pro_9.md)

## Setup / Configuration

This integration requires:

- iQua account email + password
- **Device UUID** (not the serial number)

### Where to find the Device UUID

Open the web app:

1. Login at https://app.myiquaapp.com
2. Open **My devices**
3. Select your device
4. Copy the UUID from the URL, e.g.

`https://app.myiquaapp.com/devices/<DEVICE_UUID>`

Use `<DEVICE_UUID>` in the Home Assistant config flow.

## Configuration
To add an iQua water softener to Home assistant, go to Settings and click "+ ADD INTEGRATION" button. From list select "iQua Softener" and click it, in displayed window you must enter:
- Username - username for iQua application
- Password - password for iQua application
- Device UUID number - device uuid number, you can find it in iQua MyApp Webpage after successful login on: https://app.myiquaapp.com/ the device UUID is then found after login in the URL: 'https://app.myiquaapp.com/devices/<UUID>'

## License
[MIT](https://choosealicense.com/licenses/mit/)







### Daily-Metriken (Berechnungen)

Die Integration berechnet **ausschließlich Tageswerte ("heute")**, um unterschiedliche Referenzzeitpunkte von Haus- und Enthärterzähler zu vermeiden.

- **Rohwasser heute (L)** = Haus heute − Enthärter heute
- **Rohwasseranteil heute (%)** = Rohwasser heute / Haus heute
- **Weichwasseranteil heute (%)** = 100 − Rohwasseranteil heute *(optional, standardmäßig deaktiviert)*
- **Effektive Härte heute (°dH)** = Rohwasserhärte × Rohwasseranteil

> Annahme: Das durch den Ionentauscher gelaufene Wasser wird mit **0 °dH** angenommen. Falls du eine abweichende Weichwasserhärte berücksichtigen möchtest, kann der Wert **Weichwasserhärte (°dH)** am Gerät gesetzt werden (Default: 0.0).
## Abgeleitete Werte: Effektive Härte, Verschnitt und Natrium

### Effektive Härte (geglättet)
Zusätzlich zur täglichen Berechnung der effektiven Härte stellt die Integration einen **geglätteten (EWMA)** Sensor bereit
(**Zeitkonstante ~60 Minuten**). Das reduziert Sprünge zwischen Poll-Intervallen und liefert einen stabileren „Ist“-Wert.

### Natrium (mg/L) und Grenzwert
Bei Ionenaustausch-Enthärtung steigt der Natriumgehalt abhängig von der **Enthärtungsleistung (Δ°dH)**.

Die Integration berechnet den effektiven Natriumgehalt am Ausgang (mg/L) aus:
- *Natrium im Rohwasser* (konfigurierbarer Wert, Standard 69,2 mg/L)
- *Rohwasserhärte* (°dH)
- *Effektive Härte (geglättet)* (°dH)

Formel:
`Na_eff = Na_roh + (H_roh - H_eff) * 8`

Hinweise/Quellen:
- LEYCOsoft PRO Serviceanleitung: „Um die Wasserhärte um 1 °dH zu verringern, wird **8 mg/L Natrium** zugegeben; Grenzwert **200 mg/L**.“
- Grenzwert Natrium im Trinkwasser: **200 mg/L** (Trinkwasserverordnung).

Die Integration stellt außerdem einen **Binary Sensor** bereit, der `on` wird, wenn `Na_eff > 200 mg/L`.

