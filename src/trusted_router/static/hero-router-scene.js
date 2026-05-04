"use strict";

const THREE_MODULE_URL = "https://unpkg.com/three@0.165.0/build/three.module.js";

const PROVIDERS = [
  "OpenAI",
  "Claude",
  "Gemini",
  "DeepSeek",
  "Mistral",
  "Cerebras",
  "Vertex",
  "BYOK",
];

async function loadThree() {
  return import(THREE_MODULE_URL);
}

function curveBetween(THREE, start, end, bend) {
  const midpoint = start.clone().lerp(end, 0.5);
  midpoint.y += bend;
  midpoint.z += bend * 0.28;
  return new THREE.QuadraticBezierCurve3(start, midpoint, end);
}

function makeLine(THREE, points, color, opacity) {
  const geometry = new THREE.BufferGeometry().setFromPoints(points);
  const material = new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity,
  });
  return new THREE.Line(geometry, material);
}

function initRouterScene(container, THREE) {
  const canvas = container.querySelector("[data-router-scene-canvas]");
  if (!canvas) return;

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const renderer = new THREE.WebGLRenderer({
    alpha: true,
    antialias: true,
    canvas,
    preserveDrawingBuffer: true,
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
  camera.position.set(0, 0.32, 7.2);

  const root = new THREE.Group();
  scene.add(root);

  scene.add(new THREE.AmbientLight(0xb9d7ff, 1.7));
  const keyLight = new THREE.DirectionalLight(0xffffff, 2.8);
  keyLight.position.set(3, 4, 5);
  scene.add(keyLight);
  const greenLight = new THREE.PointLight(0x19a06d, 18, 9);
  greenLight.position.set(-2.5, -1.7, 2.5);
  scene.add(greenLight);

  const gridMaterial = new THREE.LineBasicMaterial({
    color: 0x2355a6,
    transparent: true,
    opacity: 0.14,
  });
  const gridGeometry = new THREE.BufferGeometry();
  const gridPoints = [];
  for (let i = -5; i <= 5; i += 1) {
    gridPoints.push(new THREE.Vector3(-3.8, i * 0.38, -1.35));
    gridPoints.push(new THREE.Vector3(3.8, i * 0.38, -1.35));
    gridPoints.push(new THREE.Vector3(i * 0.7, -2.2, -1.35));
    gridPoints.push(new THREE.Vector3(i * 0.7, 2.2, -1.35));
  }
  gridGeometry.setFromPoints(gridPoints);
  const grid = new THREE.LineSegments(gridGeometry, gridMaterial);
  root.add(grid);

  const coreMaterial = new THREE.MeshStandardMaterial({
    color: 0x12353f,
    emissive: 0x0c6c54,
    emissiveIntensity: 0.7,
    metalness: 0.45,
    roughness: 0.26,
  });
  const core = new THREE.Mesh(new THREE.IcosahedronGeometry(0.86, 2), coreMaterial);
  root.add(core);

  const coreEdges = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(0.91, 2)),
    new THREE.LineBasicMaterial({ color: 0x7be0b1, transparent: true, opacity: 0.74 })
  );
  root.add(coreEdges);

  const shieldRing = new THREE.Mesh(
    new THREE.TorusGeometry(1.22, 0.018, 12, 96),
    new THREE.MeshBasicMaterial({ color: 0x2355a6, transparent: true, opacity: 0.45 })
  );
  shieldRing.rotation.x = Math.PI / 2.9;
  root.add(shieldRing);

  const routeStart = new THREE.Vector3(0, 0, 0);
  const nodeGroup = new THREE.Group();
  root.add(nodeGroup);
  const nodeGeometry = new THREE.OctahedronGeometry(0.16, 1);
  const ringGeometry = new THREE.TorusGeometry(0.27, 0.012, 8, 36);
  const routes = PROVIDERS.map((provider, index) => {
    const angle = (Math.PI * 2 * index) / PROVIDERS.length - Math.PI / 2;
    const radiusX = 2.7;
    const radiusY = 1.92;
    const end = new THREE.Vector3(
      Math.cos(angle) * radiusX,
      Math.sin(angle) * radiusY,
      Math.sin(angle * 1.7) * 0.55
    );
    const curve = curveBetween(THREE, routeStart, end, index % 2 === 0 ? 0.55 : -0.42);
    const line = makeLine(THREE, curve.getPoints(48), 0x2355a6, 0.28);
    root.add(line);

    const nodeMaterial = new THREE.MeshStandardMaterial({
      color: index % 3 === 0 ? 0x19a06d : index % 3 === 1 ? 0x2355a6 : 0x8f3d22,
      emissive: 0x102820,
      emissiveIntensity: 0.35,
      metalness: 0.5,
      roughness: 0.34,
    });
    const node = new THREE.Mesh(nodeGeometry, nodeMaterial);
    node.position.copy(end);
    nodeGroup.add(node);

    const ring = new THREE.Mesh(
      ringGeometry,
      new THREE.MeshBasicMaterial({ color: 0x7be0b1, transparent: true, opacity: 0.42 })
    );
    ring.position.copy(end);
    ring.lookAt(camera.position);
    nodeGroup.add(ring);

    return { provider, curve, line, node, ring };
  });

  const tokenCount = 58;
  const tokenGeometry = new THREE.TetrahedronGeometry(0.055, 0);
  const tokenMaterial = new THREE.MeshStandardMaterial({
    color: 0x7be0b1,
    emissive: 0x19a06d,
    emissiveIntensity: 1.3,
    metalness: 0.18,
    roughness: 0.28,
  });
  const tokens = new THREE.InstancedMesh(tokenGeometry, tokenMaterial, tokenCount);
  tokens.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  root.add(tokens);
  const tokenState = Array.from({ length: tokenCount }, (_, index) => ({
    route: index % routes.length,
    offset: (index * 0.137) % 1,
    speed: 0.045 + (index % 7) * 0.006,
  }));
  const dummy = new THREE.Object3D();

  let activeRoute = 0;
  let pointerX = 0;
  let pointerY = 0;
  let targetRotX = -0.1;
  let targetRotY = 0.24;
  const clock = new THREE.Clock();

  function setActiveRoute(routeIndex) {
    activeRoute = routeIndex % routes.length;
    routes.forEach((route, index) => {
      const active = index === activeRoute;
      route.line.material.color.setHex(active ? 0x19a06d : 0x2355a6);
      route.line.material.opacity = active ? 0.86 : 0.23;
      route.node.scale.setScalar(active ? 1.52 : 1);
      route.ring.scale.setScalar(active ? 1.34 : 1);
      route.ring.material.opacity = active ? 0.82 : 0.34;
    });
  }

  function onPointerMove(event) {
    const bounds = container.getBoundingClientRect();
    pointerX = ((event.clientX - bounds.left) / Math.max(bounds.width, 1) - 0.5) * 2;
    pointerY = ((event.clientY - bounds.top) / Math.max(bounds.height, 1) - 0.5) * 2;
    targetRotY = pointerX * 0.36;
    targetRotX = -0.08 + pointerY * 0.16;
    const segment = Math.floor(((pointerX + 1) / 2) * routes.length);
    setActiveRoute(Math.max(0, Math.min(routes.length - 1, segment)));
  }

  container.addEventListener("pointermove", onPointerMove, { passive: true });
  container.addEventListener("pointerdown", () => setActiveRoute(activeRoute + 1));
  setActiveRoute(0);

  function resize() {
    const rect = container.getBoundingClientRect();
    const width = Math.max(320, Math.floor(rect.width));
    const height = Math.max(320, Math.floor(rect.height));
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }

  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();

  function animate() {
    const elapsed = clock.getElapsedTime();
    const speedScale = reducedMotion ? 0.28 : 1;
    root.rotation.y += (targetRotY - root.rotation.y) * 0.045;
    root.rotation.x += (targetRotX - root.rotation.x) * 0.045;
    root.rotation.z = Math.sin(elapsed * 0.22) * 0.035;
    core.rotation.x = elapsed * 0.28 * speedScale;
    core.rotation.y = elapsed * 0.36 * speedScale;
    coreEdges.rotation.copy(core.rotation);
    shieldRing.rotation.z = elapsed * 0.18 * speedScale;
    grid.position.y = Math.sin(elapsed * 0.45) * 0.04;

    for (let i = 0; i < tokenCount; i += 1) {
      const state = tokenState[i];
      const route = routes[state.route];
      const t = (state.offset + elapsed * state.speed * speedScale) % 1;
      const point = route.curve.getPoint(t);
      const active = state.route === activeRoute;
      dummy.position.copy(point);
      dummy.rotation.set(elapsed + i, elapsed * 0.7 + i * 0.2, elapsed * 0.42 + i);
      dummy.scale.setScalar(active ? 1.55 : 0.82);
      dummy.updateMatrix();
      tokens.setMatrixAt(i, dummy.matrix);
    }
    tokens.instanceMatrix.needsUpdate = true;

    renderer.render(scene, camera);
    requestAnimationFrame(animate);
  }

  container.dataset.sceneReady = "true";
  animate();
}

async function init() {
  const containers = Array.from(document.querySelectorAll("[data-router-scene]"));
  if (!containers.length) return;
  try {
    const THREE = await loadThree();
    containers.forEach((container) => initRouterScene(container, THREE));
  } catch {
    containers.forEach((container) => {
      container.dataset.sceneReady = "fallback";
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => void init());
} else {
  void init();
}
