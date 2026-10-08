#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 Juliana Tomazini
# Licensed under the EUPL
"""
Compare language models on the namesake check, using invented cases only.

    python3 evaluate_models.py qwen3:14b gemma4:12b

Each model must already be downloaded in Ollama (ollama pull <model>) and
Ollama must be running. The script asks every model the same 24 questions about
invented people in invented articles, in six languages, and writes
model_comparison.txt and model_comparison.csv next to it.

It never opens names.txt, settings.txt or the database, and it only talks to a
model on this computer (cloud models are refused, as in the monitor itself).

Limits: 24 invented cases measure a model on this kind of question; they do not
measure it on your real matches. Check its verdicts on real matches before
relying on them.
"""

import csv
import os
import statistics
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import earshot as pm  # noqa: E402

ADDRESS = os.environ.get("MODEL_ADDRESS", "http://localhost:11434/v1")
CONTEXT = "researchers at a European university institute for social sciences, law and economics"

# (expected, language, person, note, outlet, headline, excerpt). Everyone here is invented.
CASES = [
    ("same", "en", "Marta Ellenberg", "political scientist, works on EU enlargement", "Mock Europe",
     "Western Balkans accession talks stall",
     "The talks are stuck on rule-of-law chapters, said Marta Ellenberg, a political scientist who studies EU "
     "enlargement. 'Member states want guarantees the candidates cannot yet give,' she said."),
    ("other", "en", "Marta Ellenberg", "political scientist, works on EU enlargement", "Mock Sport",
     "Ellenberg scores twice as Rovers stay top",
     "Striker Marta Ellenberg scored twice in the second half as Rovers beat City 3-1 to stay top of the "
     "women's league. The 24-year-old has now scored eleven goals this season."),
    ("same", "en", "Tomas Ricard", "economist, works on trade policy and tariffs", "Mock Business",
     "What the new tariffs mean for European exporters",
     "Tomas Ricard, an economist who has written widely on trade policy, said the tariffs would hit "
     "car-parts suppliers hardest because their margins are already thin."),
    ("other", "en", "Tomas Ricard", "economist, works on trade policy and tariffs", "Mock Local",
     "Council approves new bakery on High Street",
     "Tomas Ricard, who has run a bakery in the town for twenty years, told councillors the new shop would "
     "create six jobs. Planning permission was granted unanimously."),
    ("other", "en", "Helen Okafor", "legal scholar, works on international courts", "Mock Courts",
     "Driver jailed after crash",
     "Helen Okafor, 34, of Riverside Road, was sentenced to two years after admitting dangerous driving. "
     "The court heard she had been travelling at twice the speed limit."),
    ("same", "en", "Helen Okafor", "legal scholar, works on international courts", "Mock World",
     "Can the court enforce its own rulings?",
     "Helen Okafor, a professor of international law who studies how international courts work, said the "
     "ruling was 'legally strong but politically fragile' because the court has no means of enforcement."),
    ("same", "it", "Giulia Ferrandi", "storica, lavora sulla storia dell'integrazione europea", "Mock Italia",
     "Settant'anni dai Trattati di Roma: che cosa resta",
     "Secondo la storica Giulia Ferrandi, che da anni studia le origini dell'integrazione europea, i Trattati "
     "nacquero da un compromesso tra interessi agricoli francesi e industriali tedeschi."),
    ("other", "it", "Giulia Ferrandi", "storica, lavora sulla storia dell'integrazione europea", "Mock Cronaca",
     "Incidente in autostrada, ferita una donna",
     "Giulia Ferrandi, 52 anni, residente a Prato, e stata trasportata in ospedale dopo lo scontro tra due "
     "auto sulla A1. Le sue condizioni non sono gravi, riferiscono i soccorritori."),
    ("same", "it", "Paolo Venturini", "economista, lavora su politica monetaria e BCE", "Mock Economia",
     "Tassi, la BCE frena: le reazioni",
     "Per l'economista Paolo Venturini, esperto di politica monetaria, la decisione della BCE di mantenere i "
     "tassi invariati segnala timori per la crescita piu che per l'inflazione."),
    ("other", "it", "Paolo Venturini", "economista, lavora su politica monetaria e BCE", "Mock Sport",
     "Venturini, doppietta e prima convocazione",
     "Doppietta di Paolo Venturini nel derby: l'attaccante classe 2004 si guadagna la prima convocazione in "
     "nazionale under 21."),
    ("same", "de", "Jonas Albrecht", "Politikwissenschaftler, forscht zu Parteien und Wahlen", "Mock Deutschland",
     "Was der Wahlausgang fur die Koalition bedeutet",
     "Der Politikwissenschaftler Jonas Albrecht, der seit Jahren zu Parteien und Wahlverhalten forscht, "
     "sieht in dem Ergebnis eine Warnung an die Regierungsparteien."),
    ("other", "de", "Jonas Albrecht", "Politikwissenschaftler, forscht zu Parteien und Wahlen", "Mock Regional",
     "Neuer Kuchenchef im Hotel am See",
     "Jonas Albrecht, bisher Sous-Chef in Munchen, ubernimmt ab Oktober die Kuche des Hotels. Er setzt auf "
     "regionale Produkte und eine kleine Karte."),
    ("same", "fr", "Claire Dumoulin", "juriste, travaille sur le droit de la concurrence europeen", "Mock France",
     "Amende record contre un geant du numerique",
     "Pour la juriste Claire Dumoulin, specialiste du droit europeen de la concurrence, l'amende montre que la "
     "Commission veut appliquer pleinement les nouvelles regles du marche numerique."),
    ("other", "fr", "Claire Dumoulin", "juriste, travaille sur le droit de la concurrence europeen", "Mock Culture",
     "Claire Dumoulin, une voix pour la chanson",
     "La chanteuse Claire Dumoulin sort son troisieme album, enregistre a Lyon, et part en tournee dans toute "
     "la France au printemps."),
    ("same", "es", "Diego Arribas", "sociologo, estudia la migracion en Europa", "Mock Espana",
     "Llegadas por mar: lo que dicen los datos",
     "El sociologo Diego Arribas, que estudia las migraciones en Europa, advierte de que las cifras de un solo "
     "verano no permiten hablar de una tendencia."),
    ("other", "es", "Diego Arribas", "sociologo, estudia la migracion en Europa", "Mock Deportes",
     "Arribas renueva hasta 2029",
     "El club ha anunciado la renovacion del central Diego Arribas, que seguira en el equipo hasta 2029 tras "
     "una temporada con 34 partidos como titular."),
    ("same", "sr", "Nikola Petrovic", "politikolog, istrazuje proces pristupanja EU", "Mock Beograd",
     "Sta znaci novi izvestaj Komisije",
     "Politikolog Nikola Petrovic, koji istrazuje proces pristupanja Evropskoj uniji, kaze da izvestaj "
     "pokazuje zastoj u poglavljima o vladavini prava."),
    ("other", "sr", "Nikola Petrovic", "politikolog, istrazuje proces pristupanja EU", "Mock Sport Beograd",
     "Petrovic osvojio zlato",
     "Atleticar Nikola Petrovic osvojio je zlatnu medalju u trci na 800 metara na balkanskom prvenstvu."),
    ("same", "en", "Amira Haddad", "economist, works on energy markets", "Mock Energy",
     "Gas prices: why the winter could be calm",
     "Storage is already 90 per cent full, notes Amira Haddad, an energy economist, so prices should stay "
     "stable unless the winter is unusually cold."),
    ("other", "en", "Amira Haddad", "economist, works on energy markets", "Mock Arts",
     "Review: a debut novel about three sisters",
     "Amira Haddad's first novel follows three sisters running a restaurant in Marseille. The prose is "
     "assured, though the ending feels rushed."),
    ("other", "en", "Peter Lang", "historian, works on the Cold War", "Mock Obituaries",
     "Peter Lang, engineer, 1941-2026",
     "Peter Lang, who designed bridges across three continents, has died aged 85. He is survived by his "
     "wife and two daughters."),
    ("same", "en", "Peter Lang", "historian, works on the Cold War", "Mock History",
     "The archives that changed what we know about 1961",
     "Newly opened archives confirm that both sides feared escalation, says historian Peter Lang, whose work "
     "on the Cold War draws on Soviet and American records."),
    ("unclear", "en", "Sara Lindqvist", "political scientist, works on Nordic welfare states", "Mock News",
     "Event listing: Thursday",
     "Thursday, 7 pm: talk by Sara Lindqvist. Entry free, booking required."),
    ("unclear", "it", "Marco Bellini", "giurista, lavora su diritto costituzionale", "Mock Notizie",
     "Elenco dei premiati",
     "Tra i premiati della serata anche Marco Bellini, Lucia Neri e Andrea Fontana."),
]


def main(models):
    if not models:
        sys.exit("Name the models to compare, for example:  python3 evaluate_models.py qwen3:14b gemma4:12b")
    rows, summary = [], []
    for model in models:
        try:
            judge = pm.Judge({"model_address": ADDRESS, "model_name": model, "model_key": "",
                              "allow_remote_model": "no", "context": CONTEXT})
        except ValueError as err:
            sys.exit(str(err))
        print("\n%s: %d invented cases ..." % (model, len(CASES)))
        times, right, wrong_same, wrong_other, unclear, failed = [], 0, 0, 0, 0, 0
        for n, (expected, lang, person, note, outlet, headline, excerpt) in enumerate(CASES, 1):
            began = time.time()
            verdict, reason = judge.judge(person, note, outlet, headline, excerpt)
            seconds = time.time() - began
            if verdict == "not judged":
                failed += 1
                if failed >= 2 and n <= 2:
                    sys.exit("%s could not be reached: %s\nIs Ollama running, and has the model been "
                             "downloaded (ollama pull %s)?" % (model, reason, model))
            else:
                times.append(seconds)
            ok = verdict == expected
            right += ok
            wrong_same += (verdict == "same" and expected == "other")       # a namesake let through
            wrong_other += (verdict == "other" and expected == "same")      # the real person hidden
            unclear += (verdict == "unclear")
            rows.append([model, n, lang, expected, verdict, "yes" if ok else "no", "%.1f" % seconds, reason])
            print("  %2d %s expected %-7s got %-10s %5.1f s %s" % (n, lang, expected, verdict, seconds,
                                                                   "" if ok else "<-- wrong"))
        summary.append((model, right, wrong_same, wrong_other, unclear, failed,
                        statistics.median(times) if times else 0, sum(times)))
    lines = ["Model comparison on the namesake check, %s" % time.strftime("%d %b %Y %H:%M"),
             "%d invented cases (%d same person, %d namesake, %d with no way of telling), in English, Italian,"
             " German, French, Spanish and Serbian." % (len(CASES), sum(c[0] == "same" for c in CASES),
                                                      sum(c[0] == "other" for c in CASES),
                                                      sum(c[0] == "unclear" for c in CASES)),
             "Every model got the same questions, at temperature 0, through %s." % ADDRESS, "",
             "%-22s %8s %22s %22s %8s %10s %16s" % ("model", "correct", "namesake let through", "real person hidden",
                                                    "unclear", "no answer", "median seconds")]
    for model, right, ws, wo, un, fl, med, total in summary:
        lines.append("%-22s %5d/%-2d %22d %22d %8d %10d %16.1f" % (model, right, len(CASES), ws, wo, un, fl, med))
    lines += ["", "'Real person hidden' is the costly error: the hide box in the report would remove a genuine match.",
              "24 invented cases are a small test. A difference of one or two answers between models is not meaningful."]
    with open(os.path.join(HERE, "model_comparison.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(HERE, "model_comparison.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        out = csv.writer(fh)
        out.writerow(["model", "case", "language", "expected", "verdict", "correct", "seconds", "reason"])
        out.writerows(rows)
    print("\n" + "\n".join(lines[4:]))
    print("\nWritten: model_comparison.txt and model_comparison.csv")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    main(sys.argv[1:])
