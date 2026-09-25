// All editable details live here.

export const CONTACT = {
  email: "adv.sumitpandey.legal@gmail.com",
  phoneDisplay: "+91 77964 72986",
  phoneTel: "+917796472986",
  whatsapp: "917796472986",             // digits only, used in https://wa.me/<number>
};

// Portrait shown in the hero arch (file lives in public/). BASE_URL keeps it working on sub-path hosting.
export const PORTRAIT = `${import.meta.env.BASE_URL}portrait.jpg`;

// Bar Council of Maharashtra & Goa emblem (public/bar-council-logo.png). Shown on the hero seal and
// the Sanad card when the file exists; until then the drawn seal is used.
export const COUNCIL_LOGO = `${import.meta.env.BASE_URL}bar-council-logo.png`;

// Face-follow frames (see build plan §7). Leave frames empty until images are ready.
export const FACE = {
  cols: 5,          // number of horizontal gaze positions
  rows: 5,          // number of vertical gaze positions
  frames: [],       // row-major list of image paths, top-left = looking up-left (prefix with import.meta.env.BASE_URL)
  // OR use a sprite sheet instead of separate files:
  sprite: null,     // e.g. "/face/sprite.webp"
};
