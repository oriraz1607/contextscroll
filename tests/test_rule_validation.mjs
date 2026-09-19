import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source = await readFile(new URL('../gnome-extension/rule-validation.js', import.meta.url), 'utf8');
const {repairRule, ruleError} = await import(`data:text/javascript,${encodeURIComponent(source)}`);

const valid = {
    enabled: true,
    application: 'Firefox',
    role: '',
    name: '',
    states: ['enabled'],
    actions: ['click'],
    decision: 'native',
};
assert.equal(ruleError(valid), null);
for (const value of [null, 5, 'bad', [], {decision: 'scroll', role: 'document web', states: 'enabled'},
    {decision: 'scroll', role: 'document web', actions: [1]},
    {decision: 'scroll', role: 'document web', enabled: 'false'},
    {decision: 'unknown', role: 'document web'},
    {decision: 'native', role: '  '},
]) {
    assert.notEqual(ruleError(value), null);
    const repaired = repairRule(value);
    assert.equal(ruleError(repaired), null);
    assert.equal(repaired.enabled, false);
}
const repaired = repairRule({...valid, actions: ['click', 5], decision: 'bad'});
assert.deepEqual(repaired.actions, ['click']);
assert.equal(repaired.application, 'Firefox');
assert.equal(repaired.decision, 'native');
