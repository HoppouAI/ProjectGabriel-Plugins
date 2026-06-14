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

// General MIDI program names, index = program number 0-127. Mirrors the
// backend list in midi_utils.py so the board can offer a per-track
// instrument override.
export const GM_PROGRAMS: string[] = [
  "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano",
  "Honky-tonk Piano", "Electric Piano 1", "Electric Piano 2", "Harpsichord",
  "Clavinet",
  "Celesta", "Glockenspiel", "Music Box", "Vibraphone", "Marimba", "Xylophone",
  "Tubular Bells", "Dulcimer",
  "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ",
  "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
  "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)",
  "Electric Guitar (clean)", "Electric Guitar (muted)", "Overdriven Guitar",
  "Distortion Guitar", "Guitar Harmonics",
  "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)",
  "Fretless Bass", "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
  "Violin", "Viola", "Cello", "Contrabass", "Tremolo Strings",
  "Pizzicato Strings", "Orchestral Harp", "Timpani",
  "String Ensemble 1", "String Ensemble 2", "Synth Strings 1", "Synth Strings 2",
  "Choir Aahs", "Voice Oohs", "Synth Voice", "Orchestra Hit",
  "Trumpet", "Trombone", "Tuba", "Muted Trumpet", "French Horn",
  "Brass Section", "Synth Brass 1", "Synth Brass 2",
  "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax",
  "Oboe", "English Horn", "Bassoon", "Clarinet",
  "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle",
  "Shakuhachi", "Whistle", "Ocarina",
  "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)",
  "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)",
  "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)",
  "Pad 5 (bowed)", "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)",
  "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)",
  "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
  "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bagpipe", "Fiddle", "Shanai",
  "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko Drum",
  "Melodic Tom", "Synth Drum", "Reverse Cymbal",
  "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet",
  "Telephone Ring", "Helicopter", "Applause", "Gunshot",
];

// GM buckets its 128 programs into 16 families of 8. Used to group the
// instrument picker into optgroups so the long list stays navigable.
export const GM_GROUPS: { label: string; start: number }[] = [
  { label: "Piano", start: 0 },
  { label: "Chromatic Percussion", start: 8 },
  { label: "Organ", start: 16 },
  { label: "Guitar", start: 24 },
  { label: "Bass", start: 32 },
  { label: "Strings", start: 40 },
  { label: "Ensemble", start: 48 },
  { label: "Brass", start: 56 },
  { label: "Reed", start: 64 },
  { label: "Pipe", start: 72 },
  { label: "Synth Lead", start: 80 },
  { label: "Synth Pad", start: 88 },
  { label: "Synth Effects", start: 96 },
  { label: "Ethnic", start: 104 },
  { label: "Percussive", start: 112 },
  { label: "Sound Effects", start: 120 },
];
