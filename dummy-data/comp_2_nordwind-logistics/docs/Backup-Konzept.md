# Backup-Konzept

**Nordwind Logistics GmbH – IT-Betrieb**
Dokument: NWL-IT-KON-012 · Version 2.3 · Stand: 08.04.2025 · Verantwortlich: Ing. Sabine Hofbauer (IT-Leitung) · Klassifizierung: INTERN

> Dieses Konzept beschreibt die Datensicherung der zentral betriebenen Systeme. Lokale Sicherungen an Arbeitsplätzen sind nicht vorgesehen – Daten gehören auf die Netzlaufwerke bzw. in die freigegebenen Cloud-Ablagen.

## 1. Grundsätze

- **3-2-1-1-0-Regel:** mindestens 3 Kopien, auf 2 verschiedenen Medien, 1 Kopie an einem anderen Standort, 1 Kopie unveränderlich (immutable), 0 Fehler bei der Wiederherstellungsprüfung.
- Primäre Sicherung im RZ Linz, sekundäre Kopie im Ausweich-RZ Wels.
- Die unveränderliche Kopie liegt auf WORM-Speicher im RZ Wels (Retention Lock 21 Tage).
- Alle Sicherungen werden mit AES-256 verschlüsselt (gemäß GIS-STD-07 Cryptography Standard). Die Schlüssel werden getrennt von den Sicherungsmedien verwahrt.
- Die Backup-Infrastruktur steht in einer eigenen Verwaltungsdomäne (nicht im Office-AD). Der Zugriff auf die Backup-Konsole ist nur mit separatem Administratorkonto und MFA möglich.

## 2. Sicherungsumfang und -parameter

| System | Sicherungsart | Frequenz | RPO | Aufbewahrung |
|---|---|---|---|---|
| LVS „Kranich" (Lagerverwaltung) | Transaktionslog + Vollsicherung | stündlich / täglich 23:00 | 1 h | Tagessicherungen 30 Tage, Monatssicherungen 12 Monate |
| TMS Routio | Vollsicherung | täglich 01:00 | 24 h | 30 Tage |
| ERP | inkrementell + Vollsicherung | alle 4 h / täglich | 4 h | 30 Tage; Jahresabschlusssicherung 7 Jahre |
| Telematikplattform TeleNord (inkl. NordDrive-Backend) | Snapshot | täglich | 24 h | 14 Tage |
| Fileserver | Snapshot + Backup | stündlich (Snapshot) / täglich | 1 h | Snapshots 7 Tage, Backups 90 Tage |
| Office-Cloud (SaaS: Mail, Dateien) | Drittanbieter-Backup gem. NWG-POL-CLD-004 | täglich | 24 h | 90 Tage |
| MDM (Gerätekonfiguration) | Konfigurationsexport | täglich | 24 h | 30 Tage |

Fahrzeug-Tablets und Handscanner (MDE) werden **nicht** gesichert. Nutzdaten liegen ausschließlich serverseitig (NordDrive-Backend bzw. LVS); Geräte werden bei Defekt über das MDM neu aufgesetzt.

## 3. Restore-Tests

1. Quartalsweise Stichproben-Restore je System aus Tabelle 2 (Datei- bzw. Tabellenebene).
2. Jährlich ein vollständiger Wiederherstellungstest des LVS im Ausweich-RZ Wels. Letzter Test: 11/2024, Dauer bis zur Betriebsbereitschaft 3 h 40 min.
3. Ergebnisse werden im Testprotokoll dokumentiert, Abweichungen als Ticket erfasst.

## 4. Überwachung

- Die Frühschicht des IT-Betriebs kontrolliert täglich alle Backup-Jobs.
- Fehlgeschlagene Jobs → Ticket mit Priorität P3.
- Zwei aufeinanderfolgende Fehlschläge bei einem kritischen System (LVS, TMS, ERP) → Information an die IT-Leitung.

## 5. Verantwortlichkeiten

| Aufgabe | Verantwortlich |
|---|---|
| Betrieb der Backup-Infrastruktur | IT-Betrieb |
| Festlegung RPO / Aufbewahrung | Systemowner gemeinsam mit IT-Leitung |
| Restore-Freigabe nach Cyberangriff | ISB |
| SaaS-Backup (Office-Cloud) | IT-Betrieb, Vorgaben Group Cloud CoE |

---

Offene Punkte aus dem internen Audit 2024: Die Sicherung der Fördertechnik-Steuerungen (SPS-Programme) in Linz-Hafen erfolgt derzeit manuell durch den Hersteller nach jeder Programmänderung; eine Integration in die zentrale Sicherung ist für 2026 geplant. Die Wiederherstellung einzelner Postfächer aus dem SaaS-Backup dauert aktuell bis zu 2 Arbeitstage, weil Restore-Aufträge beim Drittanbieter manuell beauftragt werden müssen – hier wird eine Self-Service-Lösung geprüft.
