# Glossary

Words used in the [user guide](USER_GUIDE.md), in plain language.
*Polski słowniczek: [niżej](#slowniczek-pl).*

<a id="backup"></a>
### Backup (`SyncMyMod/backup/`, `*.db.ori`)
Copies of the car's own logo files, made by the USB package on the car **before** it changes
anything: the image and both databases go to `SyncMyMod/backup/` on the USB stick (once per stick
folder), and the two databases are also kept inside the car as `*.db.ori` (once). See
[How to undo](USER_GUIDE.md#how-to-undo).

<a id="csv"></a>
### CSV
A simple table file that any spreadsheet program (Excel, LibreOffice, Google Sheets) opens and
saves. **⬇ Export CSV / ⬆ Import CSV** use it for station lists.

<a id="dab"></a>
### DAB / DAB+
Digital radio. Stations are picked from a list, not by frequency. The car keeps the DAB logos in
`dab_station_logo.db`.

<a id="data-source"></a>
### Data source
An optional place on the internet (or a file of yours) the app can take station data from:
frequencies and logos. Built in: *RadioPolska.pl* (Poland) and *My CSV file* (any country). Without
a data source everything except the automatic update works. Set in **⚙ Settings → Station data**.

<a id="ecc"></a>
### ECC (Extended Country Code)
A two-character code that says which country a station is from, e.g. `E2` (Poland) or `E0`
(Germany). Together with the first character of the [PI code](#pi) it tells countries apart — that is
what the country list in the app shows (e.g. `PL (E2-3)`).

<a id="firmware"></a>
### Firmware
The software version of the car's radio. The logo image and the two databases belong together and to
one firmware, so always use the three files from your own car.

<a id="fm"></a>
### FM
Ordinary analogue radio, tuned by frequency (87.5–108 MHz). The car keeps the FM logos in
`fm_station_logo.db`.

<a id="fmods"></a>
### FMods / FMods Tools
A community add-on for Ford SYNC 3 that lets the car run install scripts from a USB stick. The
LogosUpdater package needs **FMods Tools 2.9 or newer** on the car. LogosUpdater does not install it
and is not part of it.

<a id="head-unit"></a>
### Head unit
The radio/screen unit in the dashboard — here the Ford SYNC 3.

<a id="input-set"></a>
### Input set
A folder inside the app's `inputs/` folder holding one set of the three files (for example the demo
set `inputs/00_demo`). Shown in [Advanced mode](USER_GUIDE.md#advanced-mode).

<a id="pi"></a>
### PI code (Programme Identification)
A 4-character code every FM/DAB station sends over [RDS](#rds), e.g. `3D05`. It identifies the
station; its first character is the country digit. Many radios show it in the station info. The
car's databases link logos to stations by it.

<a id="qnx6-image"></a>
### QNX6 image (`station_logos.img`)
One big file that holds all the logo pictures of the car, like a small disk. The car's system (QNX)
reads it directly. LogosUpdater can open it and build a new one on any computer.

<a id="rds"></a>
### RDS (Radio Data System)
Extra data a station sends with its signal: its name, the [PI code](#pi) and more. The car shows the
station name from RDS — the names you type in LogosUpdater are only for finding stations in the app.

<a id="sync3"></a>
### SYNC 3
Ford's infotainment system (screen + radio). LogosUpdater is made for SYNC 3 version 3.4.

<a id="syncmymod"></a>
### SyncMyMod
The folder name FMods Tools looks for on a USB stick. It holds the install script `autoinstall.sh`
and the files to install (`SyncMyMod/files/other/`). A LogosUpdater [USB package](#usb-package) is
such a folder.

<a id="usb-package"></a>
### USB package
What step ④ makes: one `SyncMyMod/` folder with the new logo image, both databases, the install
script and short instructions (`SyncMyMod/README_USB.txt`). You put that folder in the top level of
a USB stick and plug the stick into the car.

<a id="venv"></a>
### Virtual environment (venv)
A private folder (usually `.venv`) with its own copy of Python's add-on packages, used only when you
run LogosUpdater [from source](USER_GUIDE.md#from-source). It keeps those packages away from the
rest of your system. Newer Linux versions only allow installing packages this way.

<a id="working-copy"></a>
### Working copy
The copies of your three files that the app edits (in its `work/` folder). Your originals — on the
stick or wherever you loaded them from — are never changed.

---

<a id="slowniczek-pl"></a>
## Słowniczek (PL)

Słowa z [instrukcji](USER_GUIDE.pl.md), zwykłym językiem.

<a id="pl-backup"></a>
### Kopia zapasowa (`SyncMyMod/backup/`, `*.db.ori`)
Kopie własnych plików auta, które paczka USB robi w aucie, **zanim** cokolwiek zmieni: obraz i obie
bazy trafiają do `SyncMyMod/backup/` na pendrivie (raz dla danego katalogu), a obie bazy zostają też
w aucie jako `*.db.ori` (raz). Zob. [Jak cofnąć zmiany](USER_GUIDE.pl.md#jak-cofnąć-zmiany).

<a id="pl-csv"></a>
### CSV
Prosty plik z tabelą, który otworzy i zapisze każdy arkusz kalkulacyjny (Excel, LibreOffice,
Arkusze Google). **⬇ Eksport CSV / ⬆ Import CSV** używają go do list stacji.

<a id="pl-dab"></a>
### DAB / DAB+
Radio cyfrowe. Stacje wybiera się z listy, nie po częstotliwości. Logo DAB auto trzyma w
`dab_station_logo.db`.

<a id="pl-data-source"></a>
### Źródło danych
Opcjonalne miejsce w internecie (albo Twój plik), z którego aplikacja bierze dane o stacjach:
częstotliwości i logo. Wbudowane: *RadioPolska.pl* (Polska) i *Mój plik CSV* (dowolny kraj). Bez
źródła danych działa wszystko oprócz automatycznej aktualizacji. Ustawiasz w **⚙ Ustawienia → Dane
o stacjach**.

<a id="pl-ecc"></a>
### ECC (rozszerzony kod kraju)
Dwuznakowy kod kraju stacji, np. `E2` (Polska) albo `E0` (Niemcy). Razem z pierwszym znakiem
[kodu PI](#pl-pi) rozróżnia kraje — to właśnie pokazuje lista krajów w aplikacji (np. `PL (E2-3)`).

<a id="pl-firmware"></a>
### Firmware
Wersja oprogramowania radia w aucie. Obraz z logo i obie bazy pasują do siebie i do jednego
firmware'u, więc zawsze używaj trzech plików z własnego auta.

<a id="pl-fm"></a>
### FM
Zwykłe radio analogowe, strojone częstotliwością (87,5–108 MHz). Logo FM auto trzyma w
`fm_station_logo.db`.

<a id="pl-fmods"></a>
### FMods / FMods Tools
Dodatek do Ford SYNC 3 tworzony przez społeczność, dzięki któremu auto uruchamia skrypty
instalacyjne z pendrive'a. Paczka LogosUpdater wymaga w aucie **FMods Tools 2.9 lub nowszych**.
LogosUpdater ich nie instaluje i nie jest ich częścią.

<a id="pl-head-unit"></a>
### Radio (head unit)
Jednostka radia z ekranem w desce rozdzielczej — tutaj Ford SYNC 3.

<a id="pl-input-set"></a>
### Zestaw (input set)
Katalog w `inputs/` aplikacji z jednym kompletem trzech plików (np. zestaw demo `inputs/00_demo`).
Widoczny w [Trybie zaawansowanym](USER_GUIDE.pl.md#tryb-zaawansowany).

<a id="pl-pi"></a>
### Kod PI (Programme Identification)
4-znakowy kod, który każda stacja FM/DAB nadaje przez [RDS](#pl-rds), np. `3D05`. Identyfikuje
stację; pierwszy znak to cyfra kraju. Wiele radioodbiorników pokazuje go w informacjach o stacji.
Bazy auta łączą po nim logo ze stacjami.

<a id="pl-qnx6-image"></a>
### Obraz QNX6 (`station_logos.img`)
Jeden duży plik, w którym są wszystkie obrazki logo auta — jak mały dysk. System auta (QNX) czyta go
bezpośrednio. LogosUpdater potrafi go otworzyć i zbudować nowy na zwykłym komputerze.

<a id="pl-rds"></a>
### RDS (Radio Data System)
Dodatkowe dane, które stacja nadaje razem z sygnałem: nazwę, [kod PI](#pl-pi) i inne. Auto pokazuje
nazwę stacji z RDS — nazwy wpisane w LogosUpdater służą tylko do szukania stacji w aplikacji.

<a id="pl-sync3"></a>
### SYNC 3
System multimedialny Forda (ekran + radio). LogosUpdater jest przeznaczony dla SYNC 3 w wersji 3.4.

<a id="pl-syncmymod"></a>
### SyncMyMod
Nazwa katalogu, którego FMods Tools szuka na pendrivie. Zawiera skrypt instalacyjny `autoinstall.sh`
i pliki do wgrania (`SyncMyMod/files/other/`). [Paczka USB](#pl-usb-package) z LogosUpdater to taki
właśnie katalog.

<a id="pl-usb-package"></a>
### Paczka USB
To, co robi krok ④: jeden katalog `SyncMyMod/` z nowym obrazem logo, obiema bazami, skryptem
instalacyjnym i krótką instrukcją (`SyncMyMod/README_USB.txt`). Kładziesz ten katalog na samej
górze pendrive'a i wkładasz pendrive do auta.

<a id="pl-venv"></a>
### Środowisko wirtualne (venv)
Osobny katalog (zwykle `.venv`) z własną kopią dodatków do Pythona, używany tylko przy uruchamianiu
LogosUpdater [ze źródeł](USER_GUIDE.pl.md#ze-źródeł). Trzyma te dodatki z dala od reszty systemu.
Nowsze wersje Linuksa pozwalają instalować dodatki tylko w ten sposób.

<a id="pl-working-copy"></a>
### Kopia robocza
Kopie Twoich trzech plików, które aplikacja edytuje (w jej katalogu `work/`). Oryginały — na
pendrivie albo tam, skąd je wczytałeś — nigdy nie są zmieniane.
