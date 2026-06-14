// Map a GM instrument / track label to an instrument family so the board
// can color-code chips like a real DAW. Pure string matching, cheap.

export interface Family {
  key: string;
  label: string;
  color: string; // accent used for the chip
  glyph: string; // tiny unicode marker, no icon font needed
}

const FAMILIES: Record<string, Family> = {
  drums: { key: "drums", label: "Drums", color: "#ff5d8f", glyph: "\u25C9" },
  bass: { key: "bass", label: "Bass", color: "#4c7df0", glyph: "\u2261" },
  guitar: { key: "guitar", label: "Guitar", color: "#cba45b", glyph: "\u266B" },
  keys: { key: "keys", label: "Keys", color: "#3fd9c0", glyph: "\u2592" },
  strings: { key: "strings", label: "Strings", color: "#9b6be3", glyph: "\u2240" },
  brass: { key: "brass", label: "Brass", color: "#f0954c", glyph: "\u23DA" },
  reed: { key: "reed", label: "Reed/Wind", color: "#5fd1c4", glyph: "\u2371" },
  voice: { key: "voice", label: "Voice", color: "#e36ba8", glyph: "\u25CC" },
  synth: { key: "synth", label: "Synth", color: "#7c9bff", glyph: "\u2248" },
  perc: { key: "perc", label: "Percussion", color: "#ff8a5d", glyph: "\u25C8" },
  other: { key: "other", label: "Other", color: "#8a95b8", glyph: "\u25CB" },
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
