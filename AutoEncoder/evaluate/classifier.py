import os
import io
import torch
import torch.nn as nn
from AutoEncoder.utils import *
from collections import Counter
from exasol.bucketfs import Service
from tqdm import tqdm
from AutoEncoder.loss_model import loss_CEMSE
from AutoEncoder.bucketfs_client import BucketFS_client
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
from sklearn.dummy import DummyClassifier
from sklearn.model_selection import cross_val_score, RepeatedStratifiedKFold
from torch.optim.lr_scheduler import *

class ClassifierDummy:
    def __init__(self, strategy='most_frequent', n_splits=10, n_repeats=3, random_state=42):
        self.model = DummyClassifier(strategy=strategy)
        self.cv = RepeatedStratifiedKFold(n_splits=n_splits, n_repeats=n_repeats, random_state=random_state)

    def compute_scores(self, X, y):
        scores = cross_val_score(self.model, X, y, scoring='accuracy', cv=self.cv, n_jobs=-1)
        print('Mean Accuracy: %.3f (%.3f)' % (np.mean(scores), np.std(scores)))         

class ClsNNBase(nn.Module):
    def __init__(self, layers, l1_strength, l2_strength, batch_norm, dropout, device, load_method=None, weight_path=None,
                 learning_rate=1e-3, weight_decay=0):
        super(ClsNNBase, self).__init__()
        self.num_layers = len(layers)
        self.l1_strength = l1_strength
        self.l2_strength = l2_strength

        hidden_layers = []
        for i in range(self.num_layers - 1):
            hidden_layers.append(nn.Linear(layers[i], layers[i + 1]))
            if batch_norm:
                hidden_layers.append(nn.BatchNorm1d(layers[i + 1]))
            hidden_layers.append(nn.ReLU())
            if dropout is not None:
                for drop_layer, drop_chance in dropout:
                    if i == drop_layer:
                        hidden_layers.append(nn.Dropout(drop_chance))
        hidden_layers.append(nn.Linear(layers[-1], 2))
        hidden_layers.append(nn.Sigmoid())
        self.network = nn.Sequential(*hidden_layers)

        if weight_path is not None:
            if load_method == "BucketFS":
                # Load weight from BucketFS
                client = BucketFS_client()
                weight = client.download(weight_path)
            elif load_method == "local":
                # Load weight by local file
                with open(weight_path, 'rb') as file:
                    weight = io.BytesIO(file.read())
            self.load_state_dict(torch.load(weight))

        self.optimizer = torch.optim.AdamW(self.parameters(), lr=learning_rate, weight_decay=weight_decay)
        self.scheduler = StepLR(self.optimizer, step_size=4, gamma=0.1)
        self.to(device)

    def add_regularization_hook(self, module, input, output):
        l1_reg = self.l1_strength * F.l1_loss(output, torch.zeros_like(output))
        l2_reg = self.l2_strength * F.mse_loss(output, torch.zeros_like(output))
        module.register_forward_hook(None)  
        module._forward_hooks.clear()
        return output + l1_reg + l2_reg 

    def forward(self, x):
        x = self.network(x)
        return x

    def train(self,num_epochs,batch_size,patience,layers,train_loader,val_loader,onehotencoder,scaler, \
              continous_columns,categorical_columns,device,save=None):    
        best_loss = float('inf')
        best_state_dict = None
        optimizer = self.optimizer
        scheduler = self.scheduler
        counter = 0
        # Training loop
        for epoch in range(num_epochs):
            train_progress = tqdm(train_loader, desc=f'Epoch [{epoch+1}/{num_epochs}], Training Progress', position=0, leave=True)

            running_loss = 0.0
            running_sample_count = 0.0
            train_predictions = []
            train_targets = []
            for inputs,target,_  in train_progress:
                # Forward pass
                inputs = inputs.to(device)
                outputs = self(inputs)
                target = target.to(device)

                loss = nn.CrossEntropyLoss()(outputs,target)

                # Backward pass and optimization
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                # Metrics calculation
                predictions = (outputs >= 0.5).float().cpu().numpy()
                targets = target.cpu().numpy()
                train_predictions.extend(predictions)
                train_targets.extend(targets)

                running_loss += loss.item()*batch_size
                running_sample_count += inputs.shape[0]
            
            average_loss = running_loss / running_sample_count      # Final loss: multiply by batch size then averaged over all samples
            train_accuracy = accuracy_score(train_targets, train_predictions)
            train_precision = precision_score(train_targets, train_predictions,average="macro")
            train_recall = recall_score(train_targets, train_predictions,average="macro")
            train_f1 = f1_score(train_targets, train_predictions,average="macro")
            
            train_progress.set_postfix({"Training Loss": average_loss})
            train_progress.update()
            train_progress.close()

            # Calculate validation loss
            val_progress = tqdm(val_loader, desc=f'Epoch [{epoch+1}/{num_epochs}], Validation Progress', position=0, leave=True)

            val_running_loss = 0.0
            val_running_sample_count = 0.0
            val_predictions = []
            val_targets = []
            for val_inputs, val_target, _  in val_progress:
                val_inputs = val_inputs.to(device)
                val_outputs = self(val_inputs)
                val_target = val_target.to(device)

                val_loss = nn.CrossEntropyLoss()(val_outputs,val_target)

                # Metrics calculation
                predictions = (outputs >= 0.5).float().cpu().numpy()
                targets = target.cpu().numpy()
                val_predictions.extend(predictions)
                val_targets.extend(targets)

                val_running_loss += val_loss.item()*batch_size
                val_running_sample_count += val_inputs.shape[0]

            val_avg_loss = val_running_loss / val_running_sample_count
            val_accuracy = accuracy_score(val_targets, val_predictions)
            val_precision = precision_score(val_targets, val_predictions,average="macro")
            val_recall = recall_score(val_targets, val_predictions,average="macro")
            val_f1 = f1_score(val_targets, val_predictions,average="macro")

            val_progress.set_postfix({"Validation Loss": val_avg_loss})
            val_progress.update()
            val_progress.close()
            
            print(f"Epoch [{epoch+1}/{num_epochs}], Training Loss  : {average_loss:.8f}, Accuracy: {train_accuracy:.8f}, Precision: {train_precision:.8f}, Recall: {train_recall:.8f}, F1 Score: {train_f1:.8f}")
            print(f"Epoch [{epoch+1}/{num_epochs}], Validation Loss: {val_avg_loss:.8f}, Accuracy: {val_accuracy:.8f}, Precision: {val_precision:.8f}, Recall: {val_recall:.8f}, F1 Score: {val_f1:.8f}")

            # Update the learning rate
            scheduler.step()
            print(f"Epoch [{epoch+1}/{num_epochs}]: Learning Rate = {scheduler.get_last_lr()}\n")

            # Check if validation loss has improved
            if val_avg_loss < best_loss - 0.001:
                best_loss = val_avg_loss
                best_state_dict = self.state_dict()
                counter = 0
            else:
                counter += 1
            # Early stopping condition
            if counter >= patience:
                print("Early stopping triggered. Stopping training.")
                break
            # train_progress.close()

        # Save training weight 
        if (save is not None): 
            self.load_state_dict(best_state_dict)
            layers_str = '_'.join(str(item) for item in layers[1:]) #@TODO: file name hack
            file_name = f'ClsNNBase_{layers_str}.pth'
            if (save=="BucketFS"):   
                buffer = io.BytesIO()
                torch.save(self.state_dict(), buffer)
                client = BucketFS_client()
                client.upload(f'autoencoder/{file_name}', buffer)
            elif (save=="local"):
                torch.save(self.state_dict(), file_name)
                print(f'Saved weight to {file_name}')
        else:
            pass

    def test(self,model,num_epochs,batch_size,patience,layers,train_loader,val_loader,onehotencoder,scaler, \
            optimizer,scheduler,device,continous_columns,categorical_columns,save=None):
        pass            
