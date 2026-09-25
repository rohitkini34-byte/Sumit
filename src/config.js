// All editable details live here. Replace every PLACEHOLDER before going live.

export const CONTACT = {
  email: "advocate@example.com",        // PLACEHOLDER
  phoneDisplay: "+91 00000 00000",      // PLACEHOLDER
  phoneTel: "+910000000000",            // PLACEHOLDER
  whatsapp: "910000000000",             // PLACEHOLDER, digits only, used in https://wa.me/<number>
};

// Temporary portrait. Drop a real photo at public/portrait.jpg and change this to "/portrait.jpg".
export const PORTRAIT = "/portrait.svg"; // PLACEHOLDER

// Face-follow frames (see build plan §7). Leave frames empty until images are ready.
export const FACE = {
  cols: 5,          // number of horizontal gaze positions
  rows: 5,          // number of vertical gaze positions
  frames: [],       // row-major list of image paths, top-left = looking up-left
  // OR use a sprite sheet instead of separate files:
  sprite: null,     // e.g. "/face/sprite.webp"
};
