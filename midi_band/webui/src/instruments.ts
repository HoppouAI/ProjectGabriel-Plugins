// Map a GM instrument / track label to an instrument family so the board
// can color-code chips like a real DAW. Pure string matching, cheap.

import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faDrum,
  faDrumSteelpan,
  faGuitar,
  faKeyboard,
  faMicrophoneLines,
  faMusic,
  faWaveSquare,
} from "@fortawesome/free-solid-svg-icons";

export interface Family {
  key: string;
  label: string;
  color: string; // accent used for the chip
  icon: IconDefinition; // font awesome solid icon for the chip
}

const FAMILIES: Record<string, Family> = {
  drums: { key: "drums", label: "Drums", color: "#ff5d8f", icon: faDrum },
  bass: { key: "bass", label: "Bass", color: "#4c7df0", icon: faGuitar },
  guitar: { key: "guitar", label: "Guitar", color: "#cba45b", icon: faGuitar },
  keys: { key: "keys", label: "Keys", color: "#3fd9c0", icon: faKeyboard },
  strings: { key: "strings", label: "Strings", color: "#9b6be3", icon: faMusic },
  brass: { key: "brass", label: "Brass", color: "#f0954c", icon: faMusic },
  reed: { key: "reed", label: "Reed/Wind", color: "#5fd1c4", icon: faMusic },
  voice: { key: "voice", label: "Voice", color: "#e36ba8", icon: faMicrophoneLines },
  synth: { key: "synth", label: "Synth", color: "#7c9bff", icon: faWaveSquare },
  perc: { key: "perc", label: "Percussion", color: "#ff8a5d", icon: faDrumSteelpan },
  other: { key: "other", label: "Other", color: "#8a95b8", icon: faMusic },
};

const RULES: Array<[RegExp, string]> = [
  [/drum|kit|cymbal|tom|snare|kick|hat|percuss|taiko|conga|bongo|timbale/i, "perc"],
  [/bass/i, "bass"],
  [/guitar|banjo|sitar|koto|shamisen|mandolin/i, "guitar"],
  [/piano|rhodes|clav|harpsi|organ|accordion|celesta|electric piano|keyboard/i, "keys"],
  [/violin|viola|cello|contrabass|strings|fiddle|pizzicato|orchestral harp|tremolo/i, "strings"],
  [/trumpet|trombone|tuba|horn|brass|cornet|flugel/i, "brass"],
  [/sax|oboe|clarinet|flute|piccolo|bassoon|recorder|pan flute|whistle|shakuhachi|ocarina|harmonica/i, "reed"],
  [/choir|voice|vox|vocal|aahs|oohs|lead 6/i, "voice"],
  [/synth|lead|pad|fx|saw|square|charang|atmosphere|sci-fi|soundtrack/i, "synth"],
  [/glock|vibraphone|marimba|xylophone|kalimba|music box|tubular|steel drum|agogo|woodblock|bell/i, "drums"],
];

export function familyFor(label: string | undefined | null): Family {
  const s = (label || "").toLowerCase();
  if (!s) return FAMILIES.other;
  for (const [re, key] of RULES) {
    if (re.test(s)) return FAMILIES[key];
  }
  return FAMILIES.other;
}
