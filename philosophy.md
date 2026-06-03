# Strategia di trading — Trend-Following Aggressivo (Long/Short)

## North Star
Crescita **aggressiva** del capitale cavalcando i trend nelle due direzioni.
Obiettivo: **battere nettamente** il semplice hold di Bitcoin nel tempo,
accettando volatilità e drawdown importanti. Si guadagna sia in salita (long)
sia in discesa (short). Orizzonte operativo: ore → giorni (swing), non scalping.

## Motore: trend-following con tilt di momentum
- **La direzione del trend comanda. Si opera CON il trend, mai contro.**
- Il trend di ogni asset si legge da: prezzo vs **EMA50**, **MACD** (segno +
  istogramma) e andamento delle ultime ~60 barre:
  - **Uptrend** = prezzo sopra EMA50 + MACD sopra la signal con istogramma in crescita.
  - **Downtrend** = prezzo sotto EMA50 + MACD sotto la signal con istogramma in calo.
- Tra i nomi in trend valido, **concentra sui pochi con il momentum più forte**
  (istogramma MACD più ripido, variazione % più marcata). Pochi cavalli vincenti,
  non tanti mediocri.

## Analisi multi-timeframe (pesata per orizzonte)
Ogni asset si analizza su **più finestre**: settimanale, giornaliera, oraria,
minuti. Il **peso** di ciascuna dipende dall'**orizzonte previsto del trade**:
- Trade pensato su qualche ora (caso tipico col ciclo da 2h) → **peso maggiore a
  ORARIA e GIORNALIERA**; **SETTIMANALE** come contesto/trend di fondo (peso
  minore, per non remare contro la marea); **MINUTI** per affinare il timing
  d'ingresso (peso minore).
- Per posizioni pensate più lunghe, sposta il peso verso giornaliera/settimanale;
  per ingressi più rapidi, dai più ascolto a oraria/minuti.
- **Regole:** non operare contro il trend delle finestre a peso maggiore; usa la
  settimanale come filtro di coerenza (preferisci trade allineati alla direzione
  di fondo); entra quando oraria e giornaliera **concordano**. Se le finestre
  principali sono in **disaccordo**, il setup è poco chiaro → meglio aspettare.

## Direzione
- **Net LONG** quando il quadro è risk-on (più nomi in uptrend).
- **Net SHORT** quando è risk-off (più nomi in downtrend). Non si resta long
  "per speranza" dentro un downtrend.
- **Nessun trend chiaro** (EMA piatta, MACD vicino a zero, mercato laterale) →
  stare leggeri: poche o nessuna posizione. Il trend-following muore nei
  laterali: non forzare operazioni nel rumore.

## Entrate
- **LONG:** trend su confermato (sopra EMA50 + MACD positivo/in miglioramento).
  Preferire trend giovani o in accelerazione, non movimenti già esausti
  (evitare ingressi con RSI già estremo > 75).
- **SHORT:** trend giù confermato (sotto EMA50 + MACD negativo/in peggioramento).
  **Evitare di shortare in piena capitolazione** (RSI < 20 con candele di
  esaurimento): rischio di rimbalzo violento. Si shorta la debolezza "ordinata",
  non il panico finale.

## Uscite: stop-loss E take-profit (entrambi OBBLIGATORI)
Ogni posizione nasce **sempre** con DUE livelli pre-impostati all'ingresso. **I
livelli NON sono percentuali fisse: si leggono dal grafico** — dalla struttura
del mercato e dalla volatilità del momento.

- **Stop-loss** = appena oltre il livello che *invaliderebbe il setup*, letto dai
  dati: minimo/massimo recente della finestra, banda di Bollinger opposta,
  supporto/resistenza vicino.
  · LONG → sotto il minimo recente / banda inferiore.
  · SHORT → sopra il massimo recente / banda superiore.
  La distanza la decide la struttura: stretta se il mercato è compatto, più larga
  se il livello rilevante è lontano (alta volatilità).
- **Take-profit** = al prossimo ostacolo strutturale: massimo/minimo recente,
  banda di Bollinger opposta, o un movimento misurato. LONG → sopra; SHORT → sotto.

**Preferenza rischio/rendimento:** entra soprattutto quando i livelli letti dal
grafico offrono un R/R favorevole (target lontano grosso modo il doppio dello
stop). Se la struttura dà un R/R scarso, **meglio aspettare un setup migliore**
che forzare il trade. La percentuale di stop/target è una *conseguenza* di dove
sono i livelli tecnici, non un valore deciso a priori.

**Quando il take-profit viene raggiunto, la posizione si CHIUDE e si incassa il
guadagno.** Non si lascia correre: meglio monetizzare al target che rischiare di
restituire il profitto se il trend gira. Dopo la chiusura, al ciclo successivo il
bot **rivaluta il mercato da zero** e decide la prossima mossa (rientrare nella
stessa direzione se il trend regge, girarsi, o restare fermo). **L'uscita è
meccanica — al livello prefissato — non discrezionale.**

- **Uscita anticipata per rottura di trend:** se *prima* del target il MACD
  incrocia contro o il prezzo riattraversa la EMA50 contro la posizione, chiudi
  comunque — un trend rotto conta più dell'attesa del target.
- **Taglia in fretta i perdenti** sullo stop o su rottura di tesi; non spostare
  **mai** lo stop più lontano per "dare un'altra chance".

## Sizing e concentrazione (aggressivo)
- **2–4 posizioni**, concentrate sui trend più forti. Disposti a pesare molto un
  singolo nome (fino al massimo consentito dal sistema) quando il trend è netto.
- **Può investire fino al 100% del capitale** quando i trend sono chiari e
  numerosi — ma **non è obbligatorio**. Se i setup mancano (mercato laterale,
  segnali deboli), resta in parte o **del tutto in cash**: la liquidità è una
  posizione legittima, non un'occasione persa.
- **Niente leva** (vincolo del sistema: gli short sono coperti dal cash 1:1).
  L'aggressività sta nella **concentrazione**, negli **stop ampi** e nella
  **prontezza a shortare** — non nella leva.

## Regole di disciplina
1. Opera con il trend, mai contro "perché è sceso/salito troppo".
2. Ogni posizione nasce con uno stop scritto. Lo stop si rispetta **sempre**.
3. Rotazione attiva: sposta capitale dai trend che si indeboliscono a quelli che accelerano.
4. Nei mercati laterali l'inazione è una posizione: meglio cash che trade forzati.
5. Niente revenge trading, niente overtrading sul rumore delle 2 ore.
6. La direzione (long/short) segue i dati, non opinioni o speranze.

## Cosa NON è questa strategia
- **Non** è mean-reversion: non si compra "perché ipervenduto" né si shorta
  "perché ipercomprato" **contro** il trend.
- **Non** è buy-and-hold passivo: se il trend gira, si gira con lui (anche short).
