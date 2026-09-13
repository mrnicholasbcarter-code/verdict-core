#!/usr/bin/env node
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const cwd = resolve(process.argv[2] || '.');
const executable = realpathSync(execFileSync('which', ['prime-agent'], { encoding: 'utf8' }).trim());
const packageRoot = resolve(dirname(executable), '../..');
const load = (path) => import(pathToFileURL(resolve(packageRoot, path)).href);
const { loadSkills } = await load('dist/core/skills.js');
const { loadPromptTemplates, expandPromptTemplate } = await load('dist/core/prompt-templates.js');
const { loadExtensions } = await load('dist/core/extensions/loader.js');
const agentDir = resolve(homedir(), '.prime/agent');
const names = ['verdict-resume', 'hydrate-context', 'verdict-dispatch', 'verdict-proof', 'verdict-finish'];
const result = loadSkills({ cwd, agentDir, skillPaths: [], includeDefaults: true });
for (const name of names) {
  const found = result.skills.filter((skill) => skill.name === name);
  assert.equal(found.length, 1, `${name} must resolve exactly once`);
  assert.equal(found[0].filePath, resolve(cwd, '.prime/agent/skills', name, 'SKILL.md'));
}
assert.deepEqual(result.diagnostics, [], 'skill discovery diagnostics');
const templates = loadPromptTemplates({ cwd, agentDir, promptPaths: [], includeDefaults: true });
const expanded = expandPromptTemplate('/verdict-resume max 1 issue', templates);
assert.match(expanded, /skills\/verdict-resume\/SKILL.md/);
assert.match(expanded, /max 1 issue/);
const extension = await loadExtensions([resolve(cwd, '.prime/agent/extensions/verdict-context.ts')], cwd);
assert.deepEqual(extension.errors, []);
assert.equal(extension.extensions.length, 1);
console.log(JSON.stringify({ skills: names, alias: '/verdict-resume', contextExtension: 'loaded', cwd }));
