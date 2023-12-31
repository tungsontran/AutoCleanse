from torch.utils.data import Dataset
from torch.utils.data import DataLoader
from torch.nn.utils.rnn import pad_sequence
import torch

class PlainDataset(Dataset):
    def __init__(self, data):
        self.data = data

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        data = self.data.iloc[idx].values
        tensor_data = torch.stack([torch.tensor(item, dtype=torch.float32) for item in data])
        return tensor_data, idx

class ClfDataset(Dataset):
    def __init__(self, data, targets):
        self.data = data
        self.targets = targets

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        data = self.data.iloc[idx].values
        target = self.targets.iloc[idx]
        
        tensor_data = torch.stack([torch.tensor(item, dtype=torch.float32) for item in data])
        tensor_target = torch.tensor(target, dtype=torch.float32) 
        
        return tensor_data, tensor_target, idx
