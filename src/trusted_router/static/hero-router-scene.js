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

// Per-provider hues so each route's data stream is visually distinct —
// you can see at a glance which tokens are flowing to which provider.
// Node spheres + line tints + token colors all share the same palette.
const PROVIDER_HEX = [
  0x10b981, // OpenAI    teal-green
  0xf59e0b, // Claude    anthropic amber
  0x3b82f6, // Gemini    google blue
  0x8b5cf6, // DeepSeek  purple
  0xef4444, // Mistral   red
  0xec4899, // Cerebras  pink
  0xeab308, // Vertex    yellow
  0x06b6d4, // BYOK      cyan
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
  // Pulled further back so provider nodes (at scene radius ~2.7) sit
  // well inside the canvas bounds with margin — even with token
  // repulsion bobs they stay clear of the radial mask's fade zone, so
  // nothing visually clips at the canvas edges.
  camera.position.set(0, 0.32, 9.6);


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
  // Shrunk grid extents so the "platform" the scene sits on fades out
  // well before the canvas edges — visually de-emphasizes the boundary
  // so the animation doesn't read as "exploded out of a box," it just
  // reads as living in space.
  for (let i = -4; i <= 4; i += 1) {
    gridPoints.push(new THREE.Vector3(-2.2, i * 0.34, -1.35));
    gridPoints.push(new THREE.Vector3(2.2, i * 0.34, -1.35));
    gridPoints.push(new THREE.Vector3(i * 0.55, -1.5, -1.35));
    gridPoints.push(new THREE.Vector3(i * 0.55, 1.5, -1.35));
  }
  gridGeometry.setFromPoints(gridPoints);
  const grid = new THREE.LineSegments(gridGeometry, gridMaterial);
  root.add(grid);

  // Layered iridescent core:
  //   1. Smooth high-detail metallic hero sphere (hue-cycling emissive).
  //   2. A breathing displaced wireframe overlay that ripples on a noise wave.
  //   3. Inner emissive glow shell.
  //   4. Outer Fresnel rim shader that lights up the silhouette edge so the
  //      sphere reads as if illuminated from within. This is what gives the
  //      texture its “alive” quality vs. a flat ball.
  const coreMaterial = new THREE.MeshStandardMaterial({
    color: 0x16424c,
    emissive: 0x0c6c54,
    emissiveIntensity: 0.85,
    metalness: 0.95,
    roughness: 0.14,
    flatShading: false,
  });
  const coreGroup = new THREE.Group();
  root.add(coreGroup);
  const core = new THREE.Mesh(new THREE.SphereGeometry(0.86, 96, 64), coreMaterial);
  coreGroup.add(core);

  // Wireframe lattice over the sphere — vertices wobble each frame on a
  // sin/perlin-ish wave to give the texture continuous, organic motion.
  const latticeGeometry = new THREE.IcosahedronGeometry(0.9, 4);
  const latticeBasePositions = latticeGeometry.attributes.position.array.slice();
  const lattice = new THREE.Mesh(
    latticeGeometry,
    new THREE.MeshBasicMaterial({
      color: 0x7be0b1,
      wireframe: true,
      transparent: true,
      opacity: 0.55,
    })
  );
  coreGroup.add(lattice);

  const coreEdges = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(0.94, 2)),
    new THREE.LineBasicMaterial({ color: 0x7be0b1, transparent: true, opacity: 0.55 })
  );
  coreGroup.add(coreEdges);

  // Inner emissive shell — soft glowing core visible through the metallic facets.
  const coreInnerGlow = new THREE.Mesh(
    new THREE.SphereGeometry(0.78, 48, 32),
    new THREE.MeshBasicMaterial({
      color: 0x7be0b1,
      transparent: true,
      opacity: 0.18,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    })
  );
  coreGroup.add(coreInnerGlow);

  // Fresnel rim shell: ShaderMaterial with view-angle-dependent emission.
  // pow(1 - dot(N, V), p) lights the silhouette only — gives the sphere a
  // continuous glowing edge that breathes as the sphere rotates.
  const fresnelMaterial = new THREE.ShaderMaterial({
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    side: THREE.FrontSide,
    uniforms: {
      uTime: { value: 0 },
      uIntensity: { value: 1.0 },
      uColorA: { value: new THREE.Color(0x7be0b1) },
      uColorB: { value: new THREE.Color(0x2c6ecb) },
    },
    vertexShader: `
      varying vec3 vNormal;
      varying vec3 vViewDir;
      void main() {
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        vNormal = normalize(normalMatrix * normal);
        vViewDir = normalize(-mv.xyz);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: `
      varying vec3 vNormal;
      varying vec3 vViewDir;
      uniform float uTime;
      uniform float uIntensity;
      uniform vec3 uColorA;
      uniform vec3 uColorB;
      void main() {
        float fres = pow(1.0 - max(dot(vNormal, vViewDir), 0.0), 2.6);
        float pulse = 0.85 + 0.15 * sin(uTime * 1.6);
        vec3 color = mix(uColorB, uColorA, 0.5 + 0.5 * sin(uTime * 0.4));
        gl_FragColor = vec4(color * fres * pulse * uIntensity, fres);
      }
    `,
  });
  const fresnelShell = new THREE.Mesh(
    new THREE.SphereGeometry(0.94, 64, 48),
    fresnelMaterial
  );
  coreGroup.add(fresnelShell);

  // Outer additive halo — gives the impression of light bleeding from the sphere.
  const coreHalo = new THREE.Mesh(
    new THREE.SphereGeometry(1.08, 32, 24),
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

  // Globe continents overlay: load the existing world-map.svg, render it
  // to a canvas, and use it as an equirectangular texture wrapped onto
  // a sphere just above the metallic core. The SVG's transparent ocean
  // lets the iridescent core shine through, so it reads as "globe with
  // glowing core" rather than a literal Earth.
  const globeTexture = new THREE.CanvasTexture(document.createElement("canvas"));
  globeTexture.wrapS = THREE.RepeatWrapping;
  globeTexture.colorSpace = THREE.SRGBColorSpace;
  const globeMaterial = new THREE.MeshStandardMaterial({
    map: globeTexture,
    transparent: true,
    metalness: 0.4,
    roughness: 0.55,
    emissive: 0x123823,
    emissiveIntensity: 0.55,
    emissiveMap: globeTexture,
    depthWrite: false,
  });
  const globe = new THREE.Mesh(new THREE.SphereGeometry(0.88, 96, 64), globeMaterial);
  // Equirectangular textures wrap with the prime meridian at u=0; rotate
  // the sphere so the Atlantic faces forward rather than the seam.
  globe.rotation.y = Math.PI;
  coreGroup.add(globe);

  (() => {
    const W = 1024;
    const H = 512;
    const c = document.createElement("canvas");
    c.width = W;
    c.height = H;
    const ctx = c.getContext("2d");
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.decoding = "async";
    img.onload = () => {
      ctx.clearRect(0, 0, W, H);
      ctx.drawImage(img, 0, 0, W, H);
      globeTexture.image = c;
      globeTexture.needsUpdate = true;
    };
    img.onerror = () => {
      // Fall back gracefully: hide globe layer, keep iridescent core only.
      globe.visible = false;
    };
    img.src = "/static/world-map.svg";
  })();

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
  // Smooth high-detail spheres for provider nodes (was faceted octahedrons,
  // which read as jagged at hero scale). Rings get more tubular segments
  // for clean curvature.
  const nodeGeometry = new THREE.SphereGeometry(0.16, 32, 24);
  const ringGeometry = new THREE.TorusGeometry(0.27, 0.012, 16, 96);
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

    const providerHex = PROVIDER_HEX[index % PROVIDER_HEX.length];
    const nodeMaterial = new THREE.MeshStandardMaterial({
      color: providerHex,
      emissive: providerHex,
      emissiveIntensity: 0.42,
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

    return {
      provider,
      curve,
      line,
      node,
      ring,
      providerColor: new THREE.Color(providerHex),
      // Per-route 0..1 "active-ness" — lerps smoothly toward 1 when this
      // route becomes active, toward 0 otherwise. Drives line color/opacity,
      // node scale, ring scale/opacity. Avoids the snap-pop on route change.
      activeness: index === 0 ? 1 : 0,
      activenessTarget: index === 0 ? 1 : 0,
      // Idle line color is a desaturated tint of the provider color so the
      // route reads as "this provider's lane" even when inactive; active
      // lerps toward the bright brand color.
      baseColor: new THREE.Color(providerHex).lerp(new THREE.Color(0x2355a6), 0.65),
      activeColor: new THREE.Color(providerHex),
    };
  });

  // Way more tokens, faster, more variance — gives the constant
  // "data flowing" feel even when the camera isn't moving.
  const tokenCount = 132;
  const tokenGeometry = new THREE.TetrahedronGeometry(0.062, 0);
  // Material color stays white so per-instance colors (set via setColorAt)
  // come through unmodified. emissive same — emissive is multiplied by
  // emissiveIntensity but not by instanceColor in StandardMaterial, so we
  // get a uniform glow underneath the per-instance hue.
  const tokenMaterial = new THREE.MeshStandardMaterial({
    color: 0xffffff,
    emissive: 0xffffff,
    emissiveIntensity: 0.6,
    metalness: 0.18,
    roughness: 0.28,
  });
  const tokens = new THREE.InstancedMesh(tokenGeometry, tokenMaterial, tokenCount);
  tokens.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  root.add(tokens);
  const tokenState = Array.from({ length: tokenCount }, (_, index) => ({
    route: index % routes.length,
    offset: (index * 0.137) % 1,
    speed: 0.16 + ((index * 13) % 11) * 0.022,
    rotPhase: index * 0.31,
    // Bidirectional traffic: alternate tokens travel from core → node
    // (request) vs. node → core (response). Implemented by flipping
    // the curve parameter t for "in" tokens. Reads as packets going
    // both ways on every route — the routing metaphor a real router
    // does, plus visually denser traffic on each line.
    direction: index % 2 === 0 ? 1 : -1,
    // Smoothed repulsion vector — lerped toward the per-frame target
    // so the cursor's effect ramps in/out gently.
    push: new THREE.Vector3(),
  }));
  // Per-token color = its destination provider's hue. Set once at init —
  // route assignments don't change, so we don't have to update each frame.
  for (let i = 0; i < tokenCount; i += 1) {
    tokens.setColorAt(i, routes[tokenState[i].route].providerColor);
  }
  if (tokens.instanceColor) tokens.instanceColor.needsUpdate = true;
  const dummy = new THREE.Object3D();

  let activeRoute = 0;
  let pointerX = 0;
  let pointerY = 0;
  let targetRotX = -0.1;
  let targetRotY = 0.24;
  let lastPointerEvent = -Infinity;
  const clock = new THREE.Clock();

  // Smoothed targetRot — onPointerMove writes desired*; the animate
  // loop lerps target* toward desired* gradually so first-hover
  // doesn't snap the scene back from a wide idle sweep to a tight
  // pointer-tracked angle (which read as "everything collapsed").
  let desiredRotY = 0.24;
  let desiredRotX = -0.1;

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
  const tmpDesiredPush = new THREE.Vector3();
  // Auto-cycle the active route every ~2.4s so the scene keeps lighting
  // up new paths even when nobody's interacting. setActiveRoute also
  // lerps colors so the cycle reads as a wave.
  const ROUTE_AUTO_CYCLE_SECONDS = 2.4;
  let routeAutoCycleAt = 0;

  function setActiveRoute(routeIndex) {
    activeRoute = ((routeIndex % routes.length) + routes.length) % routes.length;
    routes.forEach((route, index) => {
      route.activenessTarget = index === activeRoute ? 1 : 0;
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

  // Map the cursor to the nearest provider node by projecting each node's
  // 3D position to screen space and picking the smallest pixel distance.
  // (The previous heuristic split the canvas into N horizontal slices,
  // which broke for nodes arranged in a circle — top and bottom nodes at
  // the same x landed in the same slice.)
  const projTmp = new THREE.Vector3();
  function nearestRouteToPointer(clientX, clientY) {
    const bounds = container.getBoundingClientRect();
    root.updateMatrixWorld();
    let best = activeRoute;
    let bestDist2 = Infinity;
    routes.forEach((route, index) => {
      projTmp.copy(route.node.position).applyMatrix4(root.matrixWorld).project(camera);
      const sx = bounds.left + (projTmp.x * 0.5 + 0.5) * bounds.width;
      const sy = bounds.top + (-projTmp.y * 0.5 + 0.5) * bounds.height;
      const dx = sx - clientX;
      const dy = sy - clientY;
      const d2 = dx * dx + dy * dy;
      if (d2 < bestDist2) {
        bestDist2 = d2;
        best = index;
      }
    });
    return best;
  }

  function onPointerMove(event) {
    const bounds = container.getBoundingClientRect();
    pointerX = ((event.clientX - bounds.left) / Math.max(bounds.width, 1) - 0.5) * 2;
    pointerY = ((event.clientY - bounds.top) / Math.max(bounds.height, 1) - 0.5) * 2;
    ndc.x = pointerX;
    ndc.y = -pointerY;
    mouseInside = true;
    projectMouseToWorld();
    desiredRotY = pointerX * 0.36;
    desiredRotX = -0.08 + pointerY * 0.16;
    setActiveRoute(nearestRouteToPointer(event.clientX, event.clientY));
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

  // Pointer events fire on the hit-test overlay (pentagon footprint) so
  // the canvas can overflow into adjacent text without trapping clicks.
  // Falls back to the container if the overlay isn't present.
  const hitTarget = container.querySelector("[data-router-scene-hit]") || container;
  hitTarget.addEventListener("pointermove", onPointerMove, { passive: true });
  hitTarget.addEventListener("pointerleave", onPointerLeave, { passive: true });
  hitTarget.addEventListener("pointerdown", onPointerDown);
  setActiveRoute(0);

  function resize() {
    // Canvas is sized via CSS to overflow its container (negative inset
    // for the "explode out" effect). Read the canvas's *actual* rendered
    // size so the renderer matches its display pixels — otherwise the
    // 3D scene would be rendered at container size and stretched, which
    // looks blurry.
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(320, Math.floor(rect.width));
    const height = Math.max(320, Math.floor(rect.height));
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }

  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
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
    // Idle auto-rotate: a gentle sweep so the scene never sits still.
    // Amplitude is moderate (0.32) so when the user enters with the
    // cursor we don't have to swing back from a wide idle angle to a
    // narrow pointer-tracked one — the transition feels continuous.
    // The desired* values get lerped into target* below, which gives
    // an extra layer of smoothing on the idle→pointer handoff.
    let idleY = 0.24;
    let idleX = -0.08;
    if (idleSeconds > 1.4) {
      const idleSweep = Math.min(1, (idleSeconds - 1.4) / 0.6);
      idleY = Math.sin(elapsed * 0.32) * 0.32 * idleSweep + desiredRotY * (1 - idleSweep);
      idleX = -0.08 + Math.cos(elapsed * 0.22) * 0.12 * idleSweep + desiredRotX * (1 - idleSweep);
    } else {
      idleY = desiredRotY;
      idleX = desiredRotX;
    }
    // Smooth-blend the *target* itself toward the idle/pointer-derived
    // value. This is what removes the "everything snaps back to the
    // middle" feel on first hover — the target shifts gradually
    // instead of jumping.
    targetRotY += (idleY - targetRotY) * 0.06;
    targetRotX += (idleX - targetRotX) * 0.06;
    // Tighter lerp coefficients for smoother, more responsive rotation;
    // z gets a gentle continuous roll so the scene never sits still.
    root.rotation.y += (targetRotY - root.rotation.y) * 0.075;
    root.rotation.x += (targetRotX - root.rotation.x) * 0.075;
    root.rotation.z = Math.sin(elapsed * 0.34) * 0.07;

    // Pulsing core: faster spin + scale pulse + emissive throb +
    // hue-cycling emissive so the surface reads as iridescent. Click
    // bursts add a brief scale boost on top of the baseline pulse.
    core.rotation.x = elapsed * 0.62 * speedScale;
    core.rotation.y = elapsed * 0.78 * speedScale;
    lattice.rotation.x = elapsed * 0.34 * speedScale;
    lattice.rotation.y = -elapsed * 0.46 * speedScale;
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
    fresnelMaterial.uniforms.uTime.value = elapsed;
    fresnelMaterial.uniforms.uIntensity.value = 1.0 + flashBoost * 2.5;

    // Displace lattice vertices on a 3-axis sin wave so the wireframe
    // ripples as if breathing. Cheap noise: sum of three sines per axis.
    const latticePositions = latticeGeometry.attributes.position.array;
    const baseLen = latticeBasePositions.length / 3;
    for (let i = 0; i < baseLen; i += 1) {
      const ix = i * 3;
      const bx = latticeBasePositions[ix];
      const by = latticeBasePositions[ix + 1];
      const bz = latticeBasePositions[ix + 2];
      const wave =
        Math.sin(elapsed * 1.1 + bx * 4.0) * 0.5 +
        Math.sin(elapsed * 1.4 + by * 4.0) * 0.5 +
        Math.sin(elapsed * 0.9 + bz * 4.0) * 0.5;
      const k = 1 + wave * 0.018 + flashBoost * 0.05;
      latticePositions[ix] = bx * k;
      latticePositions[ix + 1] = by * k;
      latticePositions[ix + 2] = bz * k;
    }
    latticeGeometry.attributes.position.needsUpdate = true;

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
    // Activeness lerps from 0..1 over ~0.4s for smooth transitions.
    const ROUTE_LERP = 0.085;
    routes.forEach((route, index) => {
      const phase = elapsed * (1.1 + index * 0.13) + index;
      const radial = 1 + Math.sin(phase) * 0.06;
      route.node.position.copy(route.curve.getPoint(1));
      route.node.position.multiplyScalar(radial);
      route.node.rotation.x = phase * 0.6;
      route.node.rotation.y = phase * 0.4;
      route.ring.position.copy(route.node.position);
      route.ring.rotation.z = elapsed * (0.8 + index * 0.07);

      route.activeness += (route.activenessTarget - route.activeness) * ROUTE_LERP;
      const a = route.activeness;
      route.line.material.color.copy(route.baseColor).lerp(route.activeColor, a);
      route.line.material.opacity = 0.23 + a * 0.63;
      route.node.scale.setScalar(1 + a * 0.52);
      const ringPulse = 1 + Math.sin(elapsed * 4 + index) * (0.05 + a * 0.13);
      route.ring.scale.setScalar((1 + a * 0.34) * ringPulse);
      route.ring.material.opacity = 0.34 + a * 0.48;
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

    // Token render: each token rides its curve at a constant per-token
    // speed; the only dynamic offsets are a slow scatter wobble and a
    // very gentle cursor repulsion. Repulsion is faded out near the
    // origin so tokens packed at the curve start (the central globe)
    // don't all get yanked simultaneously when the cursor is anywhere
    // near the middle — that's what made it look like popcorn.
    const repulseActive = mouseInside && elapsed - lastPointerEvent < 1.4;
    const burstShockActive = burstAge >= 0 && burstAge < 0.5;
    const SCATTER_FREQ = 0.7;
    const SCATTER_AMP = 0.035;
    const REPULSE_RADIUS = 0.85;
    const REPULSE_STRENGTH = 0.10;
    // Maximum displacement from the curve we'll ever apply.
    const PUSH_MAX_LEN = 0.12;
    // Push lerp dropped further (~25 frames to converge) so the
    // cursor effect drifts in like a tide instead of a shove.
    const PUSH_LERP = 0.04;
    for (let i = 0; i < tokenCount; i += 1) {
      const state = tokenState[i];
      const route = routes[state.route];
      const a = route.activeness;
      const stepSpeed = state.speed * speedScale * (1 + a * 0.3);
      // Bidirectional flow: even-index tokens travel out (curve t 0→1,
      // request leaving), odd-index travel in (1→0, response arriving).
      const rawT = (state.offset + elapsed * stepSpeed) % 1;
      const t = state.direction === 1 ? rawT : 1 - rawT;
      const point = route.curve.getPoint(t);
      const scatter = Math.sin(elapsed * SCATTER_FREQ + i * 0.5) * SCATTER_AMP;
      tmpVec.set(point.x, point.y + scatter, point.z);

      // Compute the *desired* push for this frame (cursor repulsion
      // + burst kick). Then lerp state.push toward it so the actual
      // applied push ramps up/down gradually instead of snapping when
      // the cursor enters/exits a token's repulse radius — that snap
      // was what read as "jerky acceleration."
      tmpDesiredPush.set(0, 0, 0);
      if (repulseActive) {
        tmpPush.copy(tmpVec).sub(mouseWorld);
        const dist = tmpPush.length();
        if (dist < REPULSE_RADIUS && dist > 0.0001) {
          const radial = Math.min(1, tmpVec.length() / 0.9);
          const falloff = (1 - dist / REPULSE_RADIUS);
          tmpPush.normalize().multiplyScalar(falloff * falloff * REPULSE_STRENGTH * radial);
          tmpDesiredPush.add(tmpPush);
        }
      }
      if (burstShockActive) {
        tmpPush.copy(tmpVec).sub(burstRing.position);
        const dist = tmpPush.length();
        const k = burstAge / 0.5;
        const waveFront = 0.3 + k * 4.0;
        if (Math.abs(dist - waveFront) < 0.6 && dist > 0.0001) {
          const intensity = (1 - Math.abs(dist - waveFront) / 0.6) * (1 - k) * 0.3;
          tmpPush.normalize().multiplyScalar(intensity);
          tmpDesiredPush.add(tmpPush);
        }
      }
      state.push.lerp(tmpDesiredPush, PUSH_LERP);
      // Hard magnitude cap so nothing ever flings far from its curve.
      if (state.push.lengthSq() > PUSH_MAX_LEN * PUSH_MAX_LEN) {
        state.push.setLength(PUSH_MAX_LEN);
      }
      tmpVec.add(state.push);

      state.rotPhase += 0.012;
      dummy.position.copy(tmpVec);
      dummy.rotation.set(state.rotPhase * 0.6, state.rotPhase, 0);
      const tokenScale = 0.9 + a * 0.95;
      dummy.scale.setScalar(tokenScale * (elapsed < burstFlashUntil ? 1.25 : 1));
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
