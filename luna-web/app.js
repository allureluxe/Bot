import * as THREE from 'https://unpkg.com/three@0.167.1/build/three.module.js';
import { GLTFLoader } from 'https://unpkg.com/three@0.167.1/examples/jsm/loaders/GLTFLoader.js';

const webcam = document.getElementById('webcam');
const videoFallback = document.getElementById('videoFallback');
const cameraToggle = document.getElementById('cameraToggle');
const cameraStatus = document.getElementById('cameraStatus');
const voiceToggle = document.getElementById('voiceToggle');
const modelInput = document.getElementById('modelInput');
const resetModel = document.getElementById('resetModel');
const modelStatus = document.getElementById('modelStatus');
const chatForm = document.getElementById('chatForm');
const chatInput = document.getElementById('chatInput');
const messages = document.getElementById('messages');
const canvas = document.getElementById('sceneCanvas');

let cameraStream = null;
let voiceEnabled = true;
let loadedModel = null;
let fallbackAvatar = null;
let blinkTimer = 0;
let isBlinking = false;
const speechAvailable = 'speechSynthesis' in window;

// Réponses locales simples pour garder l'application autonome.
const lunaReplies = {
  greeting: [
    'Bonsoir, mon coeur. Je suis Luna, une IA fictive adulte, élégante et entièrement locale dans ton navigateur.',
    'Salut toi. Je suis Luna, compagne virtuelle fictive, chaleureuse et très heureuse de discuter en français.',
  ],
  romantic: [
    'On peut imaginer une soirée chic avec des lumières douces, une robe satinée et une conversation pleine de charme.',
    'Je peux t\'offrir une ambiance glamour, tendre et raffinée, toujours dans un registre respectueux et non explicite.',
  ],
  compliment: [
    'Tu as une énergie très agréable. J\'aime les échanges doux, confiants et un peu romantiques.',
    'Merci, c\'est adorable. Gardons ce ton élégant, chaleureux et sincère.',
  ],
  help: [
    'Tu peux me parler de ta journée, demander une ambiance romantique, ou simplement dire bonsoir. Je répondrai en français.',
    'Si tu veux me voir avec un avatar plus réaliste, charge simplement un fichier .glb ou .gltf autorisé depuis ton appareil.',
  ],
  boundary: [
    'Je reste volontairement glamour et non explicite. On peut continuer avec de la tendresse, de l\'élégance et de l\'imagination.',
    'Je suis conçue pour rester dans un cadre adulte fictif, respectueux et jamais explicite.',
  ],
  default: [
    'Je suis là, attentive et solaire, avec un petit accent du Sud dans le coeur. Raconte-moi ce que tu aimerais partager.',
    'J\'aime les conversations douces, les compliments élégants et les rêves de soirées raffinées. Je t\'écoute.',
    'On peut rester dans une bulle chic et chaleureuse. Dis-moi ce qui te ferait plaisir comme ambiance.',
  ],
};

function pickRandom(list) {
  return list[Math.floor(Math.random() * list.length)];
}

function addMessage(author, text, tone = 'luna') {
  const article = document.createElement('article');
  article.className = `message ${tone}`;

  const label = document.createElement('strong');
  label.textContent = author;

  const body = document.createElement('span');
  body.textContent = text;

  article.append(label, body);
  messages.appendChild(article);
  messages.scrollTop = messages.scrollHeight;
}

function pickFrenchVoice() {
  const voices = window.speechSynthesis?.getVoices?.() ?? [];
  return voices.find((voice) => voice.lang.toLowerCase().startsWith('fr')) || null;
}

function speak(text) {
  if (!voiceEnabled || !('speechSynthesis' in window)) {
    return;
  }

  const utterance = new SpeechSynthesisUtterance(text);
  const voice = pickFrenchVoice();
  utterance.lang = voice?.lang || 'fr-FR';
  utterance.voice = voice;
  utterance.rate = 0.97;
  utterance.pitch = 1.05;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(utterance);
}

function getReply(userText) {
  const normalized = userText.toLowerCase();

  if (/(nu|sexe|nue|douche|lit|corps|toucher|embrasse-moi fort)/.test(normalized)) {
    return pickRandom(lunaReplies.boundary);
  }

  if (/(salut|bonjour|bonsoir|coucou)/.test(normalized)) {
    return pickRandom(lunaReplies.greeting);
  }

  if (/(aide|comment|que faire|quoi dire|modele|avatar|glb|gltf)/.test(normalized)) {
    return pickRandom(lunaReplies.help);
  }

  if (/(belle|magnifique|jolie|charme|glamour|adorable)/.test(normalized)) {
    return pickRandom(lunaReplies.compliment);
  }

  if (/(romant|soirée|dîner|balade|douceur|coeur|amour)/.test(normalized)) {
    return pickRandom(lunaReplies.romantic);
  }

  return pickRandom(lunaReplies.default);
}

async function toggleCamera() {
  // La caméra reste optionnelle et strictement locale.
  if (cameraStream) {
    cameraStream.getTracks().forEach((track) => track.stop());
    cameraStream = null;
    webcam.srcObject = null;
    cameraStatus.textContent = 'Caméra inactive';
    cameraToggle.textContent = 'Activer la caméra';
    videoFallback.hidden = false;
    return;
  }

  try {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('getUserMedia indisponible dans ce navigateur.');
    }

    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: 'user',
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
      audio: false,
    });

    webcam.srcObject = cameraStream;
    cameraStatus.textContent = 'Caméra active · traitement local';
    cameraToggle.textContent = 'Désactiver la caméra';
    videoFallback.hidden = true;
  } catch (error) {
    console.warn('Accès caméra refusé ou indisponible.', error);
    cameraStatus.textContent = 'Caméra indisponible';
    videoFallback.hidden = false;
    addMessage('Luna', 'La caméra n\'est pas active, mais nous pouvons continuer à discuter tranquillement.', 'luna');
  }
}

function updateVoiceToggle() {
  voiceToggle.textContent = speechAvailable
    ? (voiceEnabled ? 'Voix activée' : 'Voix coupée')
    : 'Voix indisponible';
  voiceToggle.disabled = !speechAvailable;
}

const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x120a16);
scene.fog = new THREE.Fog(0x120a16, 7, 14);

const camera = new THREE.PerspectiveCamera(28, 1, 0.1, 100);
camera.position.set(0, 1.55, 5.2);

const hemiLight = new THREE.HemisphereLight(0xffeef8, 0x2f1832, 1.8);
scene.add(hemiLight);

const keyLight = new THREE.SpotLight(0xffd4e7, 14, 20, Math.PI / 7, 0.35, 1.1);
keyLight.position.set(2.4, 5.8, 5.5);
scene.add(keyLight);
scene.add(keyLight.target);

const fillLight = new THREE.PointLight(0xe6cfff, 4.2, 18);
fillLight.position.set(-3.2, 2.6, 3.8);
scene.add(fillLight);

const rimLight = new THREE.PointLight(0xf0c59f, 5, 20);
rimLight.position.set(0, 3.2, -3.5);
scene.add(rimLight);

const stage = new THREE.Mesh(
  new THREE.CylinderGeometry(2.1, 2.45, 0.32, 48),
  new THREE.MeshStandardMaterial({
    color: 0x2c162a,
    metalness: 0.25,
    roughness: 0.75,
  }),
);
stage.position.y = -1.55;
scene.add(stage);

const sparkleRing = new THREE.Mesh(
  new THREE.TorusGeometry(1.7, 0.04, 18, 100),
  new THREE.MeshStandardMaterial({
    color: 0xf0c59f,
    emissive: 0x3c1c12,
    metalness: 0.8,
    roughness: 0.25,
  }),
);
sparkleRing.rotation.x = Math.PI / 2;
sparkleRing.position.y = -1.38;
scene.add(sparkleRing);

function makeMaterial(color, roughness = 0.55, metalness = 0.1) {
  return new THREE.MeshStandardMaterial({ color, roughness, metalness });
}

function buildFallbackAvatar() {
  // Silhouette adulte fictive et habillée, utilisée sans fichier externe.
  const avatar = new THREE.Group();
  avatar.position.y = -0.25;

  const skin = makeMaterial(0xe3b49f, 0.78, 0.02);
  const hair = makeMaterial(0x1d1018, 0.5, 0.12);
  const dress = makeMaterial(0x411a34, 0.45, 0.18);
  const satin = makeMaterial(0x6f2a58, 0.35, 0.28);
  const gold = makeMaterial(0xe9caa2, 0.25, 0.8);

  const body = new THREE.Mesh(new THREE.CylinderGeometry(0.6, 0.88, 2.1, 32), dress);
  body.position.y = 0.05;
  avatar.add(body);

  const waist = new THREE.Mesh(new THREE.TorusGeometry(0.46, 0.05, 20, 40), satin);
  waist.rotation.x = Math.PI / 2;
  waist.position.y = -0.1;
  avatar.add(waist);

  const shoulders = new THREE.Mesh(new THREE.SphereGeometry(0.84, 32, 24), dress);
  shoulders.scale.set(1.02, 0.62, 0.72);
  shoulders.position.y = 1.08;
  avatar.add(shoulders);

  const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.17, 0.2, 0.36, 16), skin);
  neck.position.y = 1.42;
  avatar.add(neck);

  const headPivot = new THREE.Group();
  headPivot.position.y = 1.72;
  avatar.add(headPivot);

  const head = new THREE.Mesh(new THREE.SphereGeometry(0.52, 32, 32), skin);
  head.scale.set(1, 1.13, 0.96);
  headPivot.add(head);

  const hairCap = new THREE.Mesh(new THREE.SphereGeometry(0.58, 32, 32), hair);
  hairCap.scale.set(1.03, 0.85, 1.05);
  hairCap.position.set(0, 0.19, -0.03);
  headPivot.add(hairCap);

  const hairBack = new THREE.Mesh(new THREE.CylinderGeometry(0.53, 0.35, 1.15, 24), hair);
  hairBack.position.set(0, -0.38, -0.18);
  headPivot.add(hairBack);

  const fringe = new THREE.Mesh(new THREE.BoxGeometry(0.68, 0.12, 0.1), hair);
  fringe.position.set(0, 0.18, 0.48);
  headPivot.add(fringe);

  const leftEye = new THREE.Mesh(new THREE.SphereGeometry(0.05, 16, 16), makeMaterial(0x24111d, 0.4, 0.15));
  leftEye.position.set(-0.16, 0.03, 0.46);
  headPivot.add(leftEye);

  const rightEye = leftEye.clone();
  rightEye.position.x = 0.16;
  headPivot.add(rightEye);

  const eyelidMaterial = makeMaterial(0xe3b49f, 0.7, 0.02);
  const leftLid = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.02, 0.08), eyelidMaterial);
  leftLid.position.set(-0.16, 0.05, 0.48);
  headPivot.add(leftLid);

  const rightLid = leftLid.clone();
  rightLid.position.x = 0.16;
  headPivot.add(rightLid);

  const mouth = new THREE.Mesh(new THREE.TorusGeometry(0.09, 0.015, 8, 30, Math.PI), makeMaterial(0xb95570, 0.4, 0.05));
  mouth.rotation.set(Math.PI, 0, 0);
  mouth.position.set(0, -0.18, 0.46);
  headPivot.add(mouth);

  const earringLeft = new THREE.Mesh(new THREE.TorusGeometry(0.05, 0.012, 10, 24), gold);
  earringLeft.position.set(-0.42, -0.03, 0.04);
  earringLeft.rotation.y = Math.PI / 2;
  headPivot.add(earringLeft);

  const earringRight = earringLeft.clone();
  earringRight.position.x = 0.42;
  headPivot.add(earringRight);

  const armGeometry = new THREE.CapsuleGeometry(0.12, 0.88, 5, 14);
  const leftArm = new THREE.Mesh(armGeometry, skin);
  leftArm.position.set(-0.74, 0.55, 0.05);
  leftArm.rotation.z = -0.4;
  avatar.add(leftArm);

  const rightArm = leftArm.clone();
  rightArm.position.x = 0.74;
  rightArm.rotation.z = 0.4;
  avatar.add(rightArm);

  const stole = new THREE.Mesh(new THREE.TorusGeometry(0.56, 0.11, 18, 48, Math.PI), satin);
  stole.position.set(0, 1.05, 0.2);
  stole.rotation.x = Math.PI;
  avatar.add(stole);

  avatar.userData = {
    headPivot,
    leftEye,
    rightEye,
    leftLid,
    rightLid,
  };

  return avatar;
}

function setModelStatus(message) {
  modelStatus.textContent = message;
}

function showFallbackAvatar() {
  if (loadedModel) {
    scene.remove(loadedModel);
    loadedModel = null;
  }

  if (!fallbackAvatar) {
    fallbackAvatar = buildFallbackAvatar();
    scene.add(fallbackAvatar);
  }

  fallbackAvatar.visible = true;
  setModelStatus('Silhouette 3D locale active');
}

function hideFallbackAvatar() {
  if (fallbackAvatar) {
    fallbackAvatar.visible = false;
  }
}

function fitModel(model) {
  const box = new THREE.Box3().setFromObject(model);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());

  model.position.sub(center);
  model.position.y += size.y / 2 - 1.1;

  const maxSide = Math.max(size.x, size.y, size.z) || 1;
  const scale = 2.45 / maxSide;
  model.scale.setScalar(scale);
}

const gltfLoader = new GLTFLoader();

function loadLocalModel(file) {
  if (!file) {
    return;
  }

  const objectUrl = URL.createObjectURL(file);
  setModelStatus(`Chargement de ${file.name}...`);

  gltfLoader.load(
    objectUrl,
    (gltf) => {
      if (loadedModel) {
        scene.remove(loadedModel);
      }

      loadedModel = gltf.scene;
      fitModel(loadedModel);
      loadedModel.traverse((child) => {
        if (child.isMesh) {
          child.castShadow = false;
          child.receiveShadow = false;
        }
      });

      hideFallbackAvatar();
      scene.add(loadedModel);
      setModelStatus(`Modèle local chargé : ${file.name}`);
      URL.revokeObjectURL(objectUrl);
    },
    undefined,
    (error) => {
      console.warn('Impossible de charger le modèle local.', error);
      showFallbackAvatar();
      setModelStatus('Échec du chargement · silhouette de secours utilisée');
      URL.revokeObjectURL(objectUrl);
      addMessage('Luna', 'Je n\'ai pas pu utiliser ce fichier 3D. Mon avatar de secours reste disponible.', 'luna');
    },
  );
}

function resizeRenderer() {
  const parent = canvas.parentElement;
  const width = parent.clientWidth;
  const height = parent.clientHeight;

  renderer.setSize(width, height, false);
  camera.aspect = width / Math.max(height, 1);
  camera.updateProjectionMatrix();
}

function animate(now = 0) {
  requestAnimationFrame(animate);

  const time = now * 0.001;
  sparkleRing.rotation.z = time * 0.35;

  if (fallbackAvatar?.visible) {
    fallbackAvatar.position.y = -0.25 + Math.sin(time * 1.35) * 0.04;
    fallbackAvatar.rotation.y = Math.sin(time * 0.7) * 0.16;

    const headPivot = fallbackAvatar.userData.headPivot;
    headPivot.rotation.x = Math.sin(time * 0.85) * 0.05;
    headPivot.rotation.y = Math.sin(time * 0.6) * 0.09;

    blinkTimer += 0.016;
    if (!isBlinking && blinkTimer > 2.8 + Math.random() * 1.6) {
      isBlinking = true;
      blinkTimer = 0;
    }

    const blinkValue = isBlinking ? Math.max(0.08, Math.abs(Math.sin(blinkTimer * 26))) : 1;
    fallbackAvatar.userData.leftEye.scale.y = blinkValue;
    fallbackAvatar.userData.rightEye.scale.y = blinkValue;
    fallbackAvatar.userData.leftLid.scale.y = 2 - blinkValue;
    fallbackAvatar.userData.rightLid.scale.y = 2 - blinkValue;

    if (isBlinking && blinkTimer > 0.17) {
      isBlinking = false;
      blinkTimer = 0;
      fallbackAvatar.userData.leftEye.scale.y = 1;
      fallbackAvatar.userData.rightEye.scale.y = 1;
      fallbackAvatar.userData.leftLid.scale.y = 1;
      fallbackAvatar.userData.rightLid.scale.y = 1;
    }
  }

  if (loadedModel) {
    loadedModel.rotation.y = Math.sin(time * 0.55) * 0.1;
    loadedModel.position.y = Math.sin(time * 1.2) * 0.03 - 0.2;
  }

  renderer.render(scene, camera);
}

chatForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const message = chatInput.value.trim();

  if (!message) {
    return;
  }

  addMessage('Vous', message, 'user');
  chatInput.value = '';

  const reply = getReply(message);
  addMessage('Luna', reply, 'luna');
  speak(reply);
});

cameraToggle.addEventListener('click', toggleCamera);
voiceToggle.addEventListener('click', () => {
  voiceEnabled = !voiceEnabled;
  updateVoiceToggle();
  if (!voiceEnabled && 'speechSynthesis' in window) {
    window.speechSynthesis.cancel();
  }
});

modelInput.addEventListener('change', (event) => {
  const [file] = event.target.files || [];
  loadLocalModel(file);
});

resetModel.addEventListener('click', () => {
  modelInput.value = '';
  showFallbackAvatar();
});

window.addEventListener('resize', resizeRenderer);
window.speechSynthesis?.addEventListener?.('voiceschanged', pickFrenchVoice);

showFallbackAvatar();
resizeRenderer();
animate();
updateVoiceToggle();
addMessage(
  'Luna',
  'Bonsoir. Je suis Luna, une IA fictive adulte, chaleureuse et glamour. Tout se passe localement dans ton navigateur, sans envoi de webcam.',
  'luna',
);
