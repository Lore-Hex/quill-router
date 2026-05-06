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

  // Iridescent multi-layered core: high-metal hero shell + inner emissive
  // glow + outer additive halo. The hero shell hue-cycles each frame so
  // the surface reads as living/alive instead of a static plastic ball.
  const coreMaterial = new THREE.MeshStandardMaterial({
    color: 0x16424c,
    emissive: 0x0c6c54,
    emissiveIntensity: 0.85,
    metalness: 0.92,
    roughness: 0.18,
  });
  const coreGroup = new THREE.Group();
  root.add(coreGroup);
  const core = new THREE.Mesh(new THREE.IcosahedronGeometry(0.86, 3), coreMaterial);
  coreGroup.add(core);

  const coreEdges = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(0.91, 2)),
    new THREE.LineBasicMaterial({ color: 0x7be0b1, transparent: true, opacity: 0.74 })
  );
  coreGroup.add(coreEdges);

  // Inner emissive shell — soft glowing core visible through the metallic facets.
  const coreInnerGlow = new THREE.Mesh(
    new THREE.IcosahedronGeometry(0.78, 2),
    new THREE.MeshBasicMaterial({
      color: 0x7be0b1,
      transparent: true,
      opacity: 0.18,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    })
  );
  coreGroup.add(coreInnerGlow);

  // Outer additive halo — gives the impression of light bleeding from the sphere.
  const coreHalo = new THREE.Mesh(
    new THREE.SphereGeometry(1.05, 32, 24),
    new THREE.MeshBasicMaterial({
      color: 0x19a06d,
      transparent: true,
      opacity: 0.14,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side: THREE.BackSide,
    })
  );
  coreGroup.add(coreHalo);

  // Click-burst shockwave torus: hidden by default, expanded + faded on
  // pointerdown so each click feels percussive and physical.
  const burstRing = new THREE.Mesh(
    new THREE.TorusGeometry(0.4, 0.022, 12, 96),
    new THREE.MeshBasicMaterial({
      color: 0x7be0b1,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    })
  );
  burstRing.visible = false;
  root.add(burstRing);
  let burstStart = -Infinity;
  let burstFlashUntil = -Infinity;

  const shieldRing = new THREE.Mesh(
    new THREE.TorusGeometry(1.22, 0.018, 12, 96),
    new THREE.MeshBasicMaterial({ color: 0x2355a6, transparent: true, opacity: 0.55 })
  );
  shieldRing.rotation.x = Math.PI / 2.9;
  root.add(shieldRing);

  // Outer counter-spinning energy ring — fatter, more glow, opposite axis.
  const energyRing = new THREE.Mesh(
    new THREE.TorusGeometry(1.62, 0.014, 10, 120),
    new THREE.MeshBasicMaterial({ color: 0x7be0b1, transparent: true, opacity: 0.55 })
  );
  energyRing.rotation.x = -Math.PI / 2.4;
  energyRing.rotation.y = Math.PI / 6;
  root.add(energyRing);

  // Third small inner ring for layered motion depth.
  const innerRing = new THREE.Mesh(
    new THREE.TorusGeometry(0.95, 0.008, 8, 64),
    new THREE.MeshBasicMaterial({ color: 0x19a06d, transparent: true, opacity: 0.5 })
  );
  innerRing.rotation.x = Math.PI / 2.2;
  innerRing.rotation.z = Math.PI / 5;
  root.add(innerRing);

  // Drifting background sparkle field — purely cosmetic motion ambient.
  const sparkleCount = 90;
  const sparkleGeometry = new THREE.BufferGeometry();
  const sparklePositions = new Float32Array(sparkleCount * 3);
  for (let i = 0; i < sparkleCount; i += 1) {
    sparklePositions[i * 3 + 0] = (Math.random() - 0.5) * 7.5;
    sparklePositions[i * 3 + 1] = (Math.random() - 0.5) * 4.6;
    sparklePositions[i * 3 + 2] = (Math.random() - 0.5) * 3.0 - 0.4;
  }
  sparkleGeometry.setAttribute("position", new THREE.BufferAttribute(sparklePositions, 3));
  const sparkleMaterial = new THREE.PointsMaterial({
    color: 0x7be0b1,
    size: 0.045,
    transparent: true,
    opacity: 0.5,
    sizeAttenuation: true,
  });
  const sparkles = new THREE.Points(sparkleGeometry, sparkleMaterial);
  root.add(sparkles);
  const sparkleSeeds = Array.from({ length: sparkleCount }, () => ({
    phase: Math.random() * Math.PI * 2,
    speed: 0.4 + Math.random() * 0.6,
    amp: 0.05 + Math.random() * 0.16,
  }));

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

  // Way more tokens, faster, more variance — gives the constant
  // "data flowing" feel even when the camera isn't moving.
  const tokenCount = 132;
  const tokenGeometry = new THREE.TetrahedronGeometry(0.062, 0);
  const tokenMaterial = new THREE.MeshStandardMaterial({
    color: 0x7be0b1,
    emissive: 0x19a06d,
    emissiveIntensity: 1.55,
    metalness: 0.18,
    roughness: 0.28,
  });
  const tokens = new THREE.InstancedMesh(tokenGeometry, tokenMaterial, tokenCount);
  tokens.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  root.add(tokens);
  const tokenState = Array.from({ length: tokenCount }, (_, index) => ({
    route: index % routes.length,
    offset: (index * 0.137) % 1,
    // 3.5× faster baseline, with more spread between fast and slow
    // tokens so the stream looks heterogeneous rather than synchronized.
    speed: 0.16 + ((index * 13) % 11) * 0.022,
  }));
  const dummy = new THREE.Object3D();

  let activeRoute = 0;
  let pointerX = 0;
  let pointerY = 0;
  let targetRotX = -0.1;
  let targetRotY = 0.24;
  let lastPointerEvent = -Infinity;
  const clock = new THREE.Clock();

  // Raycaster + z=0 plane to project the mouse into world space, so
  // tokens can physically push away from the cursor as it moves.
  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const cursorPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
  const mouseWorld = new THREE.Vector3(999, 999, 0); // far away by default
  let mouseInside = false;
  const REPULSE_RADIUS = 1.05;
  const REPULSE_STRENGTH = 0.55;
  const tmpVec = new THREE.Vector3();
  const tmpPush = new THREE.Vector3();
  // Auto-cycle the active route every ~2.4s so the scene keeps lighting
  // up new paths even when nobody's interacting. setActiveRoute also
  // lerps colors so the cycle reads as a wave.
  const ROUTE_AUTO_CYCLE_SECONDS = 2.4;
  let routeAutoCycleAt = 0;

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

  function projectMouseToWorld() {
    raycaster.setFromCamera(ndc, camera);
    const localOrigin = raycaster.ray.origin.clone().applyMatrix4(root.matrixWorld.clone().invert());
    const localDir = raycaster.ray.direction.clone().transformDirection(root.matrixWorld.clone().invert());
    const localRay = new THREE.Ray(localOrigin, localDir);
    const hit = new THREE.Vector3();
    if (localRay.intersectPlane(cursorPlane, hit)) {
      mouseWorld.copy(hit);
    }
  }

  function onPointerMove(event) {
    const bounds = container.getBoundingClientRect();
    pointerX = ((event.clientX - bounds.left) / Math.max(bounds.width, 1) - 0.5) * 2;
    pointerY = ((event.clientY - bounds.top) / Math.max(bounds.height, 1) - 0.5) * 2;
    ndc.x = pointerX;
    ndc.y = -pointerY;
    mouseInside = true;
    projectMouseToWorld();
    targetRotY = pointerX * 0.36;
    targetRotX = -0.08 + pointerY * 0.16;
    const segment = Math.floor(((pointerX + 1) / 2) * routes.length);
    setActiveRoute(Math.max(0, Math.min(routes.length - 1, segment)));
    lastPointerEvent = clock.getElapsedTime();
  }

  function onPointerLeave() {
    mouseInside = false;
    mouseWorld.set(999, 999, 0);
  }

  function onPointerDown(event) {
    onPointerMove(event);
    setActiveRoute(activeRoute + 1);
    burstStart = clock.getElapsedTime();
    burstRing.visible = true;
    burstRing.position.copy(mouseInside ? mouseWorld : new THREE.Vector3(0, 0, 0));
    burstFlashUntil = burstStart + 0.4;
  }

  container.addEventListener("pointermove", onPointerMove, { passive: true });
  container.addEventListener("pointerleave", onPointerLeave, { passive: true });
  container.addEventListener("pointerdown", onPointerDown);
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

    // Idle auto-rotate: when the pointer hasn't moved in a while, slow
    // sinusoidal sweep so the scene never sits still. As soon as the
    // pointer comes back the lerp blends out to the pointer-driven
    // target rotation.
    const idleSeconds = elapsed - lastPointerEvent;
    if (idleSeconds > 1.4) {
      const idleSweep = Math.min(1, (idleSeconds - 1.4) / 0.6);
      targetRotY = Math.sin(elapsed * 0.32) * 0.55 * idleSweep + targetRotY * (1 - idleSweep);
      targetRotX = -0.08 + Math.cos(elapsed * 0.22) * 0.18 * idleSweep + targetRotX * (1 - idleSweep);
    }
    root.rotation.y += (targetRotY - root.rotation.y) * 0.045;
    root.rotation.x += (targetRotX - root.rotation.x) * 0.045;
    root.rotation.z = Math.sin(elapsed * 0.34) * 0.07;

    // Pulsing core: faster spin + scale pulse + emissive throb +
    // hue-cycling emissive so the surface reads as iridescent. Click
    // bursts add a brief scale boost on top of the baseline pulse.
    core.rotation.x = elapsed * 0.62 * speedScale;
    core.rotation.y = elapsed * 0.78 * speedScale;
    const burstAge = elapsed - burstStart;
    const flashBoost = burstAge >= 0 && burstAge < 0.4
      ? (1 - burstAge / 0.4) * 0.22
      : 0;
    const corePulse = 1 + Math.sin(elapsed * 1.8 * speedScale) * 0.07 + flashBoost;
    coreGroup.scale.setScalar(corePulse);
    coreEdges.rotation.copy(core.rotation);
    // Hue cycle through teal → green → cyan via HSL.
    const hue = 0.42 + Math.sin(elapsed * 0.55) * 0.08;
    coreMaterial.emissive.setHSL(hue, 0.65, 0.32);
    coreMaterial.emissiveIntensity = 0.7 + Math.sin(elapsed * 2.4 * speedScale) * 0.4 + flashBoost * 1.4;
    coreInnerGlow.material.opacity = 0.16 + Math.sin(elapsed * 1.6) * 0.05 + flashBoost * 0.5;
    coreHalo.scale.setScalar(1 + Math.sin(elapsed * 0.9) * 0.05 + flashBoost * 0.6);
    coreHalo.material.opacity = 0.12 + Math.sin(elapsed * 1.1) * 0.03 + flashBoost * 0.5;

    // Three counter-rotating rings on different axes — depth of motion.
    shieldRing.rotation.z = elapsed * 0.42 * speedScale;
    shieldRing.rotation.y = elapsed * 0.18 * speedScale;
    energyRing.rotation.z = -elapsed * 0.55 * speedScale;
    energyRing.rotation.x = -Math.PI / 2.4 + Math.sin(elapsed * 0.6) * 0.18;
    innerRing.rotation.z = elapsed * 0.92 * speedScale;
    innerRing.rotation.y = -elapsed * 0.28 * speedScale;

    grid.position.y = Math.sin(elapsed * 0.7) * 0.06;
    grid.rotation.z = Math.sin(elapsed * 0.18) * 0.04;

    // Auto-cycle active route every ROUTE_AUTO_CYCLE_SECONDS — keeps
    // the green active path moving even when the cursor is off-canvas.
    if (idleSeconds > 0.8 && elapsed - routeAutoCycleAt > ROUTE_AUTO_CYCLE_SECONDS) {
      setActiveRoute(activeRoute + 1);
      routeAutoCycleAt = elapsed;
    }

    // Per-route node bob + ring pulse (each at a unique frequency so
    // the eye sees independent motion, not a synchronized wobble).
    routes.forEach((route, index) => {
      const phase = elapsed * (1.1 + index * 0.13) + index;
      const radial = 1 + Math.sin(phase) * 0.06;
      route.node.position.copy(route.curve.getPoint(1));
      route.node.position.multiplyScalar(radial);
      route.node.rotation.x = phase * 0.6;
      route.node.rotation.y = phase * 0.4;
      route.ring.position.copy(route.node.position);
      route.ring.rotation.z = elapsed * (0.8 + index * 0.07);
      const isActive = index === activeRoute;
      const ringPulse = 1 + Math.sin(elapsed * 4 + index) * (isActive ? 0.18 : 0.05);
      route.ring.scale.setScalar((isActive ? 1.34 : 1) * ringPulse);
    });

    // Click-burst shockwave: torus expands ~0 → 2.4 units over 0.7s,
    // fading as it grows. Hidden once the cycle is done.
    if (burstAge >= 0 && burstAge < 0.7) {
      const k = burstAge / 0.7;
      burstRing.visible = true;
      burstRing.scale.setScalar(0.3 + k * 6.0);
      burstRing.material.opacity = (1 - k) * 0.85;
      burstRing.rotation.x = Math.PI / 2;
      burstRing.rotation.z = elapsed * 0.5;
    } else if (burstRing.visible) {
      burstRing.visible = false;
    }

    // Tokens with banded coloring per route + active-route brightness
    // boost + cursor repulsion (tokens push away from the projected
    // mouse position when it's within REPULSE_RADIUS units).
    const repulseActive = mouseInside && elapsed - lastPointerEvent < 1.4;
    const burstShockActive = burstAge >= 0 && burstAge < 0.5;
    for (let i = 0; i < tokenCount; i += 1) {
      const state = tokenState[i];
      const route = routes[state.route];
      const active = state.route === activeRoute;
      const stepSpeed = state.speed * speedScale * (active ? 1.3 : 1);
      const t = (state.offset + elapsed * stepSpeed) % 1;
      const point = route.curve.getPoint(t);
      // Subtle radial scatter so the stream reads like a swarm, not a thread.
      const scatter = Math.sin(elapsed * 1.7 + i * 0.6) * 0.04;
      tmpVec.set(point.x, point.y + scatter, point.z);

      if (repulseActive) {
        tmpPush.copy(tmpVec).sub(mouseWorld);
        const dist = tmpPush.length();
        if (dist < REPULSE_RADIUS && dist > 0.0001) {
          const falloff = (1 - dist / REPULSE_RADIUS);
          tmpPush.normalize().multiplyScalar(falloff * falloff * REPULSE_STRENGTH);
          tmpVec.add(tmpPush);
        }
      }
      if (burstShockActive) {
        // Outward kick from burst origin during the first half of the
        // shockwave — tokens get visibly knocked along the wave.
        tmpPush.copy(tmpVec).sub(burstRing.position);
        const dist = tmpPush.length();
        const k = burstAge / 0.5;
        const waveFront = 0.3 + k * 4.0;
        if (Math.abs(dist - waveFront) < 0.6 && dist > 0.0001) {
          const intensity = (1 - Math.abs(dist - waveFront) / 0.6) * (1 - k) * 0.35;
          tmpPush.normalize().multiplyScalar(intensity);
          tmpVec.add(tmpPush);
        }
      }

      dummy.position.copy(tmpVec);
      dummy.rotation.set(elapsed * 1.1 + i, elapsed * 1.4 + i * 0.2, elapsed * 0.7 + i);
      dummy.scale.setScalar((active ? 1.85 : 0.9) * (elapsed < burstFlashUntil ? 1.25 : 1));
      dummy.updateMatrix();
      tokens.setMatrixAt(i, dummy.matrix);
    }
    tokens.instanceMatrix.needsUpdate = true;

    // Sparkle field: each point oscillates on its own seed so the
    // background never freezes.
    const positions = sparkles.geometry.attributes.position.array;
    for (let i = 0; i < sparkleCount; i += 1) {
      const seed = sparkleSeeds[i];
      positions[i * 3 + 1] = positions[i * 3 + 1] + Math.sin(elapsed * seed.speed + seed.phase) * seed.amp * 0.012;
    }
    sparkles.geometry.attributes.position.needsUpdate = true;
    sparkles.rotation.z = elapsed * 0.04;

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
