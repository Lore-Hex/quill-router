// Maple evidence page: the three ways to check (AI agent, Commands, Sources) as kit Tabs.
// Without JavaScript the tab row stays hidden and all three panels show, one after another.
(() => {
  'use strict';
  const list = document.querySelector('.mv3-tabs');
  if (!list) return;
  const tabs = [...list.querySelectorAll('[role="tab"]')];
  const panels = tabs.map(tab => document.getElementById(tab.getAttribute('aria-controls')));
  if (panels.some(panel => !panel)) return;
  function select(tab, focus) {
    tabs.forEach((other, index) => {
      const on = other === tab;
      other.setAttribute('aria-selected', String(on));
      other.tabIndex = on ? 0 : -1;
      panels[index].hidden = !on;
    });
    if (focus) tab.focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(tab, false));
    tab.addEventListener('keydown', event => {
      const step = {ArrowRight: 1, ArrowLeft: -1}[event.key];
      let next = null;
      if (step) next = tabs[(index + step + tabs.length) % tabs.length];
      else if (event.key === 'Home') next = tabs[0];
      else if (event.key === 'End') next = tabs[tabs.length - 1];
      if (!next) return;
      event.preventDefault();
      select(next, true);
    });
  });
  panels.forEach(panel => { panel.tabIndex = 0; });
  list.hidden = false;
  select(tabs[0], false);
})();
