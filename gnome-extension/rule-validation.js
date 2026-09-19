const TEXT_FIELDS = ['application', 'role', 'name'];
const LIST_FIELDS = ['states', 'actions'];

export function ruleError(rule) {
    if (rule === null || typeof rule !== 'object' || Array.isArray(rule))
        return 'Rule must be an object';
    if (rule.decision !== 'native' && rule.decision !== 'scroll')
        return 'Result must be native or scroll';
    if (rule.enabled !== undefined && typeof rule.enabled !== 'boolean')
        return 'Enabled must be true or false';
    for (const field of TEXT_FIELDS) {
        if (rule[field] !== undefined && typeof rule[field] !== 'string')
            return `${field} must be text`;
    }
    for (const field of LIST_FIELDS) {
        if (rule[field] !== undefined &&
            (!Array.isArray(rule[field]) ||
             rule[field].some(item => typeof item !== 'string')))
            return `${field} must be a text array`;
    }
    if (!TEXT_FIELDS.some(field => rule[field]?.trim()) &&
        !LIST_FIELDS.some(field => rule[field]?.some(item => item.trim())))
        return 'At least one matcher is required';
    return null;
}

export function repairRule(value) {
    const source = value !== null && typeof value === 'object' &&
        !Array.isArray(value) ? value : {};
    const repaired = {
        enabled: false,
        application: typeof source.application === 'string' ? source.application : '',
        role: typeof source.role === 'string' ? source.role : '',
        name: typeof source.name === 'string' ? source.name : '',
        states: Array.isArray(source.states)
            ? source.states.filter(item => typeof item === 'string').slice(0, 32) : [],
        actions: Array.isArray(source.actions)
            ? source.actions.filter(item => typeof item === 'string').slice(0, 32) : [],
        decision: source.decision === 'scroll' ? 'scroll' : 'native',
    };
    if (ruleError(repaired) !== null)
        repaired.role = 'document web';
    return repaired;
}
